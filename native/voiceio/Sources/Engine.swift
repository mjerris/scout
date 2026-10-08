// The audio side: one AVAudioEngine with voice processing (AUVoiceProcessingIO)
// owning both the microphone and the speakers, so the canceller sees exactly
// what is played. Everything except the tap callback runs on the state queue.

import AVFoundation
import Foundation

protocol Backend: AnyObject {
    func start(_ config: Config)
    func play(_ request: PlayRequest)
    func stop()
    func quit()
}

/// Counters written from the tap thread and read from the state queue.
final class MicCounters: @unchecked Sendable {
    private let lock = NSLock()
    private var chunks = 0
    private var samples = 0
    private var lastChunk = 0
    private var maxChunk = 0
    private var errors = 0

    func record(inputFrames: Int, outSamples: Int) {
        lock.lock()
        chunks += 1
        samples += outSamples
        lastChunk = inputFrames
        maxChunk = max(maxChunk, inputFrames)
        lock.unlock()
    }

    func recordError() {
        lock.lock()
        errors += 1
        lock.unlock()
    }

    func snapshot() -> [String: Any] {
        lock.lock()
        defer { lock.unlock() }
        return [
            "mic_chunks": chunks,
            "mic_samples": samples,
            "mic_seconds": Double(samples) / 16000,
            "mic_last_chunk_frames": lastChunk,
            "mic_max_chunk_frames": maxChunk,
            "mic_convert_errors": errors,
        ]
    }
}

func micPermission() -> String {
    switch AVCaptureDevice.authorizationStatus(for: .audio) {
    case .authorized: return "authorized"
    case .denied: return "denied"
    case .restricted: return "restricted"
    case .notDetermined: return "not_determined"
    @unknown default: return "unknown"
    }
}

/// Starts the shared stats timer: every `interval` seconds `fields()` is sent as a stats event.
func startStatsTimer(queue: DispatchQueue, output: Output, interval: Double,
                     _ fields: @escaping () -> [String: Any]) -> DispatchSourceTimer
{
    let timer = DispatchSource.makeTimerSource(queue: queue)
    timer.schedule(deadline: .now() + interval, repeating: interval)
    timer.setEventHandler { output.event("stats", fields()) }
    timer.resume()
    return timer
}

final class EngineBackend: Backend {
    private let output: Output
    private let queue: DispatchQueue
    private let tracker: PlaybackTracker
    private let counters = MicCounters()
    private let started = Date()
    private var config = Config()

    private var engine: AVAudioEngine?
    private var player: AVAudioPlayerNode?
    private var playConverter: PlayConverter?
    private var observer: NSObjectProtocol?
    private var statsTimer: DispatchSourceTimer?

    // What the current build resolved, to tell a real device change from noise.
    private var builtInput: AudioDeviceID?
    private var builtOutput: AudioDeviceID?
    private var builtDefaultInput: AudioDeviceID?
    private var builtDefaultOutput: AudioDeviceID?
    private var info: [String: Any] = [:]

    private var rebuildScheduled = false
    private var rebuilds: [Date] = []
    private var rebuildCount = 0
    private var playErrors = 0

    init(output: Output, queue: DispatchQueue) {
        self.output = output
        self.queue = queue
        tracker = PlaybackTracker(output: output)
    }

    // MARK: Backend

    func start(_ config: Config) {
        self.config = config
        do {
            info = try build()
        } catch {
            fail("cannot start audio: \(error)")
        }
        output.event("ready", info)
        Devices.watch(queue: queue) { [weak self] reason in self?.devicesChanged(reason) }
        statsTimer = startStatsTimer(queue: queue, output: output, interval: 10) { [weak self] in
            self?.stats() ?? [:]
        }
    }

    func play(_ request: PlayRequest) {
        guard let player, let playConverter, request.samples.count > 0 else {
            tracker.add(request.id)
            tracker.finish(request.id, played: request.samples.count == 0)
            return
        }
        let buffer: AVAudioPCMBuffer
        do {
            buffer = try playConverter.buffer(for: request)
        } catch {
            playErrors += 1
            log("play \(request.id): \(error)")
            tracker.add(request.id)
            tracker.finish(request.id, played: false)
            return
        }
        tracker.add(request.id)
        let id = request.id
        // .dataPlayedBack fires once the audio has actually left the device, so
        // "done" means the room heard it. player.stop() also fires it; by then the
        // id has been reported stopped and finish() ignores it.
        player.scheduleBuffer(buffer, at: nil, options: [], completionCallbackType: .dataPlayedBack) {
            [weak self] _ in
            self?.queue.async { self?.tracker.finish(id, played: true) }
        }
        if !player.isPlaying {
            player.play()
        }
    }

    func stop() {
        tracker.stopAll()
        guard let player else { return }
        player.stop()
        player.play()
    }

    func quit() {
        statsTimer?.cancel()
        tracker.stopAll()
        teardown()
        output.flush()
        exit(0)
    }

    // MARK: Engine lifecycle

    private func resolve(_ name: String, input: Bool) -> (AudioDevice?, Bool) {
        if name.isEmpty { return (nil, true) }
        if let device = Devices.find(name, input: input) { return (device, true) }
        log("no \(input ? "input" : "output") device matches \"\(name)\"; using the default")
        return (nil, false)
    }

    /// A fresh engine: voice processing on, devices selected, then wired and started.
    private func build() throws -> [String: Any] {
        let engine = AVAudioEngine()
        let input = engine.inputNode
        let outputNode = engine.outputNode

        // Enabling on the input node enables it on the output node too (one
        // AUVoiceProcessingIO unit). Must happen with the engine stopped.
        try input.setVoiceProcessingEnabled(true)
        input.isVoiceProcessingBypassed = !config.voiceProcessing
        input.isVoiceProcessingAGCEnabled = config.agc
        if #available(macOS 14.0, *) {
            // Don't turn down other apps' audio (music, videos) while we listen.
            input.voiceProcessingOtherAudioDuckingConfiguration =
                AVAudioVoiceProcessingOtherAudioDuckingConfiguration(enableAdvancedDucking: false, duckingLevel: .min)
        }

        let (wantIn, inMatched) = resolve(config.inputDevice, input: true)
        let (wantOut, outMatched) = resolve(config.outputDevice, input: false)
        if let wantIn {
            guard let unit = input.audioUnit else { throw VoiceIOError("input node has no audio unit") }
            try Devices.setCurrent(unit, device: wantIn.id, element: 1)
        }
        if let wantOut {
            guard let unit = outputNode.audioUnit else { throw VoiceIOError("output node has no audio unit") }
            try Devices.setCurrent(unit, device: wantOut.id, element: 0)
        }

        let player = AVAudioPlayerNode()
        engine.attach(player)
        let playFormat = try wire(engine, player)

        observer = NotificationCenter.default.addObserver(
            forName: .AVAudioEngineConfigurationChange, object: engine, queue: nil
        ) { [weak self] _ in
            self?.queue.async { self?.scheduleRebuild("engine configuration changed", devicesChanged: false) }
        }
        self.engine = engine
        self.player = player
        playConverter = PlayConverter(to: playFormat)

        builtInput = wantIn?.id ?? Devices.defaultDevice(input: true)
        builtOutput = wantOut?.id ?? Devices.defaultDevice(input: false)
        builtDefaultInput = Devices.defaultDevice(input: true)
        builtDefaultOutput = Devices.defaultDevice(input: false)
        var fields = describeEngine(engine)
        fields["input_device_matched"] = inMatched
        fields["output_device_matched"] = outMatched
        return fields
    }

    /// Connect the graph at the formats the engine reports now, install the mic
    /// tap and start. Used for a fresh engine and to restart one after a
    /// configuration change (formats can change, e.g. the voice-processing
    /// input going from 2 to 4 channels). Returns the player's format.
    private func wire(_ engine: AVAudioEngine, _ player: AVAudioPlayerNode) throws -> AVAudioFormat {
        let input = engine.inputNode
        let outputNode = engine.outputNode
        let micFormat = input.outputFormat(forBus: 0)
        guard micFormat.sampleRate > 0, micFormat.channelCount > 0 else {
            throw VoiceIOError("the input has no usable format (no input device?)")
        }
        let hardwareOut = outputNode.outputFormat(forBus: 0)
        let playRate = hardwareOut.sampleRate > 0 ? hardwareOut.sampleRate : 48000
        guard let playFormat = AVAudioFormat(standardFormatWithSampleRate: playRate, channels: 1) else {
            throw VoiceIOError("cannot make a \(playRate) Hz playback format")
        }

        // The implicit mixer -> output connection keeps the mixer's 44.1 kHz
        // default, and the voice-processing unit then fails to initialize
        // (-10875) when the device runs at another rate. Connect it explicitly
        // at the output node's own input format.
        let outputIn = outputNode.inputFormat(forBus: 0)
        engine.connect(engine.mainMixerNode, to: outputNode, format: outputIn.sampleRate > 0 ? outputIn : nil)
        engine.connect(player, to: engine.mainMixerNode, format: playFormat)

        let mic = try MicConverter(from: micFormat)
        let output = self.output
        let counters = self.counters
        // ~20 ms requested; the engine delivers ~100 ms buffers regardless.
        let tapFrames = AVAudioFrameCount(micFormat.sampleRate * 0.02)
        input.installTap(onBus: 0, bufferSize: tapFrames, format: micFormat) { buffer, _ in
            do {
                let data = try mic.convert(buffer)
                counters.record(inputFrames: Int(buffer.frameLength), outSamples: data.count / 2)
                if !data.isEmpty { output.send(Frame.mic, data) }
            } catch {
                counters.recordError()
                log("\(error)")
            }
        }

        engine.prepare()
        do {
            try engine.start()
        } catch {
            input.removeTap(onBus: 0)
            throw VoiceIOError("engine start failed: \(error)")
        }
        player.play()
        return playFormat
    }

    private func describeEngine(_ engine: AVAudioEngine) -> [String: Any] {
        let input = engine.inputNode
        let outputNode = engine.outputNode
        var ducking: [String: Any] = ["supported": false]
        if #available(macOS 14.0, *) {
            let actual = input.voiceProcessingOtherAudioDuckingConfiguration
            ducking = [
                "supported": true,
                "advanced": actual.enableAdvancedDucking.boolValue,
                "level": actual.duckingLevel.rawValue,
            ]
        }
        let usedIn = input.audioUnit.flatMap { Devices.current($0, element: 1) }
        let usedOut = outputNode.audioUnit.flatMap { Devices.current($0, element: 0) }
        let devices = Devices.all()
        var fields: [String: Any] = [
            "input_device": builtInput.map(Devices.name) ?? "",
            "output_device": builtOutput.map(Devices.name) ?? "",
            "input_unit_device": usedIn.map(Devices.name) ?? "",
            "output_unit_device": usedOut.map(Devices.name) ?? "",
            "requested_input_device": config.inputDevice,
            "requested_output_device": config.outputDevice,
            "input_format": describe(input.outputFormat(forBus: 0)),
            "output_format": describe(outputNode.outputFormat(forBus: 0)),
            "mic_format": describe(MicConverter.outFormat),
            "voice_processing": input.isVoiceProcessingEnabled && outputNode.isVoiceProcessingEnabled,
            "voice_processing_bypassed": input.isVoiceProcessingBypassed,
            "agc": input.isVoiceProcessingAGCEnabled,
            "ducking": ducking,
            "input_latency_ms": input.presentationLatency * 1000,
            "output_latency_ms": outputNode.presentationLatency * 1000,
            "mic_permission": micPermission(),
            "inputs": devices.filter(\.inputs).map(\.name),
            "outputs": devices.filter(\.outputs).map(\.name),
        ]
        if let format = playConverter?.outFormat {
            fields["player_format"] = describe(format)
        }
        return fields
    }

    private func teardown() {
        if let observer {
            NotificationCenter.default.removeObserver(observer)
        }
        observer = nil
        if let engine {
            engine.inputNode.removeTap(onBus: 0)
            player?.stop()
            engine.stop()
        }
        engine = nil
        player = nil
        playConverter = nil
    }

    // MARK: Device changes

    private func devicesChanged(_ reason: String) {
        guard engine != nil else { return }
        var changed = false
        if config.inputDevice.isEmpty {
            changed = changed || Devices.defaultDevice(input: true) != builtDefaultInput
        } else {
            changed = changed || Devices.find(config.inputDevice, input: true)?.id != builtInput
        }
        if config.outputDevice.isEmpty {
            changed = changed || Devices.defaultDevice(input: false) != builtDefaultOutput
        } else {
            changed = changed || Devices.find(config.outputDevice, input: false)?.id != builtOutput
        }
        if changed {
            scheduleRebuild(reason, devicesChanged: true)
        }
    }

    private var pendingDevicesChanged = false

    /// Coalesce bursts of notifications (one unplug posts several) into one rebuild.
    private func scheduleRebuild(_ reason: String, devicesChanged: Bool) {
        pendingDevicesChanged = pendingDevicesChanged || devicesChanged
        guard !rebuildScheduled else { return }
        rebuildScheduled = true
        queue.asyncAfter(deadline: .now() + 0.3) { [weak self] in self?.rebuild(reason) }
    }

    /// After a configuration change on the same devices, first try restarting
    /// the existing engine (fast; voice processing stays set up). When the
    /// devices changed, or the restart fails, build a new engine.
    private func rebuild(_ reason: String) {
        rebuildScheduled = false
        let devicesChanged = pendingDevicesChanged
        pendingDevicesChanged = false
        let now = Date()
        rebuilds = rebuilds.filter { now.timeIntervalSince($0) < 30 } + [now]
        if rebuilds.count > 8 {
            fail("audio configuration keeps changing (\(rebuilds.count) restarts in 30 s)")
        }
        tracker.stopAll()
        if !devicesChanged, let engine, let player {
            log("restarting the engine: \(reason)")
            engine.inputNode.removeTap(onBus: 0)
            player.stop()
            engine.stop()
            do {
                let playFormat = try wire(engine, player)
                playConverter = PlayConverter(to: playFormat)
                announce(reason, how: "restart", devicesChanged: false)
                return
            } catch {
                log("restart failed (\(error)); building a new engine")
            }
        }
        log("rebuilding the engine: \(reason)")
        teardown()
        var lastError: Error = VoiceIOError("unknown")
        for attempt in 0..<5 {
            if attempt > 0 { Thread.sleep(forTimeInterval: 0.5) }
            do {
                info = try build()
                announce(reason, how: "rebuild", devicesChanged: devicesChanged)
                return
            } catch {
                lastError = error
                log("rebuild attempt \(attempt + 1) failed: \(error)")
                teardown()
            }
        }
        fail("cannot restart audio after \(reason): \(lastError)")
    }

    private func announce(_ reason: String, how: String, devicesChanged: Bool) {
        rebuildCount += 1
        if let engine {
            let matched = (info["input_device_matched"], info["output_device_matched"])
            info = describeEngine(engine)
            info["input_device_matched"] = matched.0
            info["output_device_matched"] = matched.1
        }
        var fields = info
        fields["reason"] = reason
        fields["restart"] = how
        fields["devices_changed"] = devicesChanged
        log("audio \(how) done (\(reason))")
        output.event("device_changed", fields)
    }


    private func fail(_ message: String) -> Never {
        log(message)
        tracker.stopAll()
        output.event("failed", ["error": message])
        output.flush()
        exit(1)
    }

    private func stats() -> [String: Any] {
        var fields = counters.snapshot()
        fields["played"] = tracker.played
        fields["stopped"] = tracker.stopped
        fields["queued"] = tracker.pending.count
        fields["play_errors"] = playErrors
        fields["engine_running"] = engine?.isRunning ?? false
        fields["rebuilds"] = rebuildCount
        fields["uptime"] = Date().timeIntervalSince(started)
        if let engine {
            fields["input_latency_ms"] = engine.inputNode.presentationLatency * 1000
            fields["output_latency_ms"] = engine.outputNode.presentationLatency * 1000
        }
        return fields
    }
}

/// --self-test: the whole protocol and both converters, but no audio devices.
/// The "microphone" is a 48 kHz 440 Hz tone; playback completes after the
/// clip's duration in wall-clock time.
final class SelfTestBackend: Backend {
    private let output: Output
    private let queue: DispatchQueue
    private let tracker: PlaybackTracker
    private let counters = MicCounters()
    private let micFormat = AVAudioFormat(standardFormatWithSampleRate: 48000, channels: 2)!
    private let playFormat = AVAudioFormat(standardFormatWithSampleRate: 48000, channels: 1)!
    private var playConverter: PlayConverter
    private var mic: MicConverter?
    private var micTimer: DispatchSourceTimer?
    private var statsTimer: DispatchSourceTimer?
    private var phase = 0.0
    private var generation = 0
    /// Clips play one after another, like the real player node's queue.
    private var playheadEnd = DispatchTime.now()

    init(output: Output, queue: DispatchQueue) {
        self.output = output
        self.queue = queue
        tracker = PlaybackTracker(output: output)
        playConverter = PlayConverter(to: playFormat)
    }

    func start(_ config: Config) {
        do {
            mic = try MicConverter(from: micFormat)
        } catch {
            output.event("failed", ["error": "\(error)"])
            output.flush()
            exit(1)
        }
        output.event("ready", [
            "self_test": true,
            "input_device": "self-test",
            "output_device": "self-test",
            "requested_input_device": config.inputDevice,
            "requested_output_device": config.outputDevice,
            "input_format": describe(micFormat),
            "output_format": describe(playFormat),
            "player_format": describe(playFormat),
            "mic_format": describe(MicConverter.outFormat),
            "voice_processing": config.voiceProcessing,
            "agc": config.agc,
        ])
        let timer = DispatchSource.makeTimerSource(queue: queue)
        timer.schedule(deadline: .now(), repeating: 0.1)
        timer.setEventHandler { [weak self] in self?.micTick() }
        timer.resume()
        micTimer = timer
        statsTimer = startStatsTimer(queue: queue, output: output, interval: 1) { [weak self] in
            guard let self else { return [:] }
            var fields = self.counters.snapshot()
            fields["played"] = self.tracker.played
            fields["stopped"] = self.tracker.stopped
            fields["queued"] = self.tracker.pending.count
            return fields
        }
    }

    private func micTick() {
        guard let mic, let buffer = toneBuffer(format: micFormat, frames: 4800, frequency: 440,
                                               amplitude: 0.1, phase: &phase)
        else { return }
        do {
            let data = try mic.convert(buffer)
            counters.record(inputFrames: 4800, outSamples: data.count / 2)
            output.send(Frame.mic, data)
        } catch {
            counters.recordError()
            log("\(error)")
        }
    }

    func play(_ request: PlayRequest) {
        tracker.add(request.id)
        let seconds: Double
        do {
            let buffer = try playConverter.buffer(for: request)
            seconds = Double(buffer.frameLength) / playFormat.sampleRate
        } catch {
            log("play \(request.id): \(error)")
            tracker.finish(request.id, played: false)
            return
        }
        let id = request.id
        let gen = generation
        let begin = max(playheadEnd, DispatchTime.now())
        playheadEnd = begin + seconds
        queue.asyncAfter(deadline: playheadEnd) { [weak self] in
            guard let self, self.generation == gen else { return }
            self.tracker.finish(id, played: true)
        }
    }

    func stop() {
        generation += 1
        playheadEnd = DispatchTime.now()
        tracker.stopAll()
    }

    func quit() {
        micTimer?.cancel()
        statsTimer?.cancel()
        tracker.stopAll()
        output.flush()
        exit(0)
    }
}
