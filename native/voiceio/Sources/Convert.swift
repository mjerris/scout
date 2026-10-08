// Sample-rate and format conversion with AVAudioConverter (no devices involved,
// so --self-test exercises exactly this code).

import AVFoundation

struct VoiceIOError: Error, CustomStringConvertible {
    let description: String
    init(_ description: String) { self.description = description }
}

func describe(_ format: AVAudioFormat) -> [String: Any] {
    let common: String
    switch format.commonFormat {
    case .pcmFormatFloat32: common = "float32"
    case .pcmFormatFloat64: common = "float64"
    case .pcmFormatInt16: common = "int16"
    case .pcmFormatInt32: common = "int32"
    default: common = "other"
    }
    return [
        "sample_rate": format.sampleRate,
        "channels": Int(format.channelCount),
        "format": common,
        "interleaved": format.isInterleaved,
    ]
}

/// Microphone path: whatever the input node delivers -> 16 kHz mono int16.
final class MicConverter: @unchecked Sendable {
    static let outFormat = AVAudioFormat(
        commonFormat: .pcmFormatInt16, sampleRate: 16000, channels: 1, interleaved: true)!

    let inFormat: AVAudioFormat
    private let converter: AVAudioConverter

    init(from format: AVAudioFormat) throws {
        guard let converter = AVAudioConverter(from: format, to: MicConverter.outFormat) else {
            throw VoiceIOError("no converter from \(format) to 16 kHz mono int16")
        }
        // With voice processing the first channel carries the processed signal;
        // take it alone rather than mixing in any other channels.
        if format.channelCount > 1 {
            converter.channelMap = [0]
        }
        inFormat = format
        self.converter = converter
    }

    /// Convert one tap buffer. Streaming: the resampler keeps its state between calls.
    func convert(_ buffer: AVAudioPCMBuffer) throws -> Data {
        let ratio = MicConverter.outFormat.sampleRate / inFormat.sampleRate
        let capacity = AVAudioFrameCount((Double(buffer.frameLength) * ratio).rounded(.up)) + 64
        guard let out = AVAudioPCMBuffer(pcmFormat: MicConverter.outFormat, frameCapacity: capacity) else {
            throw VoiceIOError("cannot allocate mic buffer")
        }
        var supplied = false
        var error: NSError?
        let status = converter.convert(to: out, error: &error) { _, inputStatus in
            if supplied {
                inputStatus.pointee = .noDataNow
                return nil
            }
            supplied = true
            inputStatus.pointee = .haveData
            return buffer
        }
        if status == .error {
            throw VoiceIOError("mic conversion failed: \(error.map { "\($0)" } ?? "unknown")")
        }
        let frames = Int(out.frameLength)
        guard frames > 0, let channel = out.int16ChannelData else { return Data() }
        return Data(bytes: channel[0], count: frames * 2)
    }
}

/// Playback path: float32 mono at any rate -> the player node's format.
final class PlayConverter {
    let outFormat: AVAudioFormat
    private var converters: [Double: AVAudioConverter] = [:]

    init(to format: AVAudioFormat) {
        outFormat = format
    }

    func buffer(for request: PlayRequest) throws -> AVAudioPCMBuffer {
        guard let inFormat = AVAudioFormat(standardFormatWithSampleRate: request.sampleRate, channels: 1),
            let input = AVAudioPCMBuffer(
                pcmFormat: inFormat, frameCapacity: AVAudioFrameCount(max(request.samples.count, 1)))
        else {
            throw VoiceIOError("cannot allocate a \(request.sampleRate) Hz buffer")
        }
        input.frameLength = AVAudioFrameCount(request.samples.count)
        request.samples.withUnsafeBufferPointer { src in
            if let base = src.baseAddress, src.count > 0 {
                input.floatChannelData![0].update(from: base, count: src.count)
            }
        }
        if inFormat.sampleRate == outFormat.sampleRate && outFormat.channelCount == 1 {
            return input
        }
        let converter: AVAudioConverter
        if let cached = converters[request.sampleRate] {
            converter = cached
            converter.reset()
        } else {
            guard let made = AVAudioConverter(from: inFormat, to: outFormat) else {
                throw VoiceIOError("no converter from \(request.sampleRate) Hz to \(outFormat)")
            }
            converters[request.sampleRate] = made
            converter = made
        }
        let ratio = outFormat.sampleRate / inFormat.sampleRate
        let capacity = AVAudioFrameCount((Double(request.samples.count) * ratio).rounded(.up)) + 64
        guard let out = AVAudioPCMBuffer(pcmFormat: outFormat, frameCapacity: capacity) else {
            throw VoiceIOError("cannot allocate playback buffer")
        }
        // A whole clip at once: supply it, then end the stream so the resampler flushes its tail.
        var supplied = false
        var error: NSError?
        let status = converter.convert(to: out, error: &error) { _, inputStatus in
            if supplied {
                inputStatus.pointee = .endOfStream
                return nil
            }
            supplied = true
            inputStatus.pointee = .haveData
            return input
        }
        if status == .error {
            throw VoiceIOError("playback conversion failed: \(error.map { "\($0)" } ?? "unknown")")
        }
        return out
    }
}

/// A test tone, for the self-test's synthetic microphone.
func toneBuffer(format: AVAudioFormat, frames: Int, frequency: Double, amplitude: Float, phase: inout Double)
    -> AVAudioPCMBuffer?
{
    guard let buffer = AVAudioPCMBuffer(pcmFormat: format, frameCapacity: AVAudioFrameCount(frames)) else {
        return nil
    }
    buffer.frameLength = AVAudioFrameCount(frames)
    let step = 2 * Double.pi * frequency / format.sampleRate
    for c in 0..<Int(format.channelCount) {
        var p = phase
        let data = buffer.floatChannelData![c]
        for i in 0..<frames {
            data[i] = amplitude * Float(sin(p))
            p += step
        }
    }
    phase = (phase + step * Double(frames)).truncatingRemainder(dividingBy: 2 * Double.pi)
    return buffer
}
