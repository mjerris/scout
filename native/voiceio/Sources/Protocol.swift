// Wire protocol between scout.audio_apple and this helper.
//
// Every message, both directions, is one binary frame:
//     [1 byte type][u32 little-endian payload length][payload]
//
// Python -> helper
//   C  config JSON {"input_device", "output_device", "voice_processing", "agc"}
//   P  play: u32 id, u32 sample_rate, float32 mono samples (little-endian)
//   S  stop playback: drop everything queued, every pending id is reported stopped
//   Q  quit
// Helper -> Python
//   M  mic audio: 16 kHz mono int16 little-endian, any chunk size
//   D  playback done: u32 id, u8 1 = played fully / 0 = stopped
//   E  event JSON {"event": "ready" | "device_changed" | "failed" | "stats", ...}
//
// Logs go to stderr only; stdout carries frames and nothing else.

import Foundation

enum Frame {
    static let config = UInt8(ascii: "C")
    static let play = UInt8(ascii: "P")
    static let stop = UInt8(ascii: "S")
    static let quit = UInt8(ascii: "Q")
    static let mic = UInt8(ascii: "M")
    static let done = UInt8(ascii: "D")
    static let event = UInt8(ascii: "E")
    /// Refuse absurd lengths rather than allocating them (a 10-minute 48 kHz clip is ~115 MB).
    static let maxPayload = 256 * 1024 * 1024
}

func log(_ message: String) {
    FileHandle.standardError.write(Data(("voiceio: " + message + "\n").utf8))
}

func appendU32(_ data: inout Data, _ value: UInt32) {
    withUnsafeBytes(of: value.littleEndian) { data.append(contentsOf: $0) }
}

func readU32(_ data: Data, at offset: Int) -> UInt32 {
    let start = data.startIndex + offset
    var value: UInt32 = 0
    for i in 0..<4 {
        value |= UInt32(data[start + i]) << (8 * UInt32(i))
    }
    return value
}

/// All stdout writes go through one serial queue, so frames from the audio
/// thread, the state queue and timers never interleave.
final class Output: @unchecked Sendable {
    private let queue = DispatchQueue(label: "voiceio.stdout")

    func send(_ type: UInt8, _ payload: Data) {
        var frame = Data(capacity: 5 + payload.count)
        frame.append(type)
        appendU32(&frame, UInt32(payload.count))
        frame.append(payload)
        queue.async { Output.writeAll(frame) }
    }

    func done(id: UInt32, played: Bool) {
        var payload = Data(capacity: 5)
        appendU32(&payload, id)
        payload.append(played ? 1 : 0)
        send(Frame.done, payload)
    }

    func event(_ name: String, _ fields: [String: Any] = [:]) {
        var object = fields
        object["event"] = name
        do {
            let json = try JSONSerialization.data(withJSONObject: object, options: [.sortedKeys])
            send(Frame.event, json)
        } catch {
            log("cannot encode \(name) event: \(error)")
        }
    }

    /// Block until everything queued so far has been written.
    func flush() {
        queue.sync {}
    }

    private static func writeAll(_ data: Data) {
        data.withUnsafeBytes { (raw: UnsafeRawBufferPointer) in
            guard let base = raw.baseAddress else { return }
            var offset = 0
            while offset < raw.count {
                let n = write(STDOUT_FILENO, base + offset, raw.count - offset)
                if n < 0 {
                    if errno == EINTR { continue }
                    // The parent is gone; there is nobody left to serve.
                    exit(0)
                }
                offset += n
            }
        }
    }
}

/// Blocking reader for stdin frames; runs on its own thread.
final class Input {
    /// Returns nil at end of input or on a read error.
    static func readExactly(_ count: Int) -> Data? {
        if count == 0 { return Data() }
        var buffer = Data(count: count)
        var offset = 0
        while offset < count {
            let n = buffer.withUnsafeMutableBytes { (raw: UnsafeMutableRawBufferPointer) -> Int in
                read(STDIN_FILENO, raw.baseAddress! + offset, count - offset)
            }
            if n == 0 { return nil }
            if n < 0 {
                if errno == EINTR { continue }
                return nil
            }
            offset += n
        }
        return buffer
    }

    /// One frame, or nil at end of input. A malformed frame is also treated as the end.
    static func readFrame() -> (UInt8, Data)? {
        guard let header = readExactly(5) else { return nil }
        let length = Int(readU32(header, at: 1))
        if length > Frame.maxPayload {
            log("frame length \(length) is too large; giving up on the stream")
            return nil
        }
        guard let payload = readExactly(length) else { return nil }
        return (header[header.startIndex], payload)
    }
}

struct Config: Decodable {
    var inputDevice: String = ""
    var outputDevice: String = ""
    var voiceProcessing: Bool = true
    var agc: Bool = true

    enum CodingKeys: String, CodingKey {
        case inputDevice = "input_device"
        case outputDevice = "output_device"
        case voiceProcessing = "voice_processing"
        case agc
    }

    init() {}

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        inputDevice = try c.decodeIfPresent(String.self, forKey: .inputDevice) ?? ""
        outputDevice = try c.decodeIfPresent(String.self, forKey: .outputDevice) ?? ""
        voiceProcessing = try c.decodeIfPresent(Bool.self, forKey: .voiceProcessing) ?? true
        agc = try c.decodeIfPresent(Bool.self, forKey: .agc) ?? true
    }
}

struct PlayRequest {
    let id: UInt32
    let sampleRate: Double
    let samples: [Float]

    /// nil when the payload is malformed.
    init?(_ payload: Data) {
        guard payload.count >= 8, (payload.count - 8) % 4 == 0 else { return nil }
        id = readU32(payload, at: 0)
        let rate = readU32(payload, at: 4)
        guard rate > 0 else { return nil }
        sampleRate = Double(rate)
        let count = (payload.count - 8) / 4
        var samples = [Float](repeating: 0, count: count)
        _ = samples.withUnsafeMutableBytes { (dest: UnsafeMutableRawBufferPointer) in
            payload.copyBytes(to: dest, from: (payload.startIndex + 8)..<payload.endIndex)
        }
        self.samples = samples
    }
}

/// Which play ids are still queued. Only touched on the state queue.
final class PlaybackTracker {
    private let output: Output
    private(set) var pending = Set<UInt32>()
    private(set) var played = 0
    private(set) var stopped = 0

    init(output: Output) {
        self.output = output
    }

    func add(_ id: UInt32) {
        pending.insert(id)
    }

    /// Report one id as finished; ignored when it was already reported (e.g. stopped).
    func finish(_ id: UInt32, played fully: Bool) {
        guard pending.remove(id) != nil else { return }
        if fully { played += 1 } else { stopped += 1 }
        output.done(id: id, played: fully)
    }

    /// Report every pending id as stopped.
    func stopAll() {
        for id in pending.sorted() {
            finish(id, played: false)
        }
    }
}
