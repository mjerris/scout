// voiceio: full-duplex audio for claude-voice with macOS voice processing.
//
//   voiceio              open the audio devices once a C (config) frame arrives
//   voiceio --self-test  same protocol, no audio devices (synthetic mic, timed playback)
//
// See Protocol.swift for the wire format. The helper exits when stdin closes,
// so it never outlives the Python process that started it.

import Foundation

signal(SIGPIPE, SIG_IGN)

let selfTest = CommandLine.arguments.contains("--self-test")
let output = Output()
let state = DispatchQueue(label: "voiceio.state")
let backend: Backend =
    selfTest ? SelfTestBackend(output: output, queue: state) : EngineBackend(output: output, queue: state)
var configured = false

func handle(_ type: UInt8, _ payload: Data) {
    switch type {
    case Frame.config:
        if configured {
            log("ignoring a second config frame")
            return
        }
        let config: Config
        do {
            config = try JSONDecoder().decode(Config.self, from: payload)
        } catch {
            output.event("failed", ["error": "bad config: \(error)"])
            output.flush()
            exit(1)
        }
        configured = true
        backend.start(config)
    case Frame.play:
        guard let request = PlayRequest(payload) else {
            log("ignoring a malformed play frame (\(payload.count) bytes)")
            return
        }
        if !configured {
            log("play \(request.id) before config; reporting it stopped")
            output.done(id: request.id, played: false)
            return
        }
        backend.play(request)
    case Frame.stop:
        if configured { backend.stop() }
    case Frame.quit:
        backend.quit()
    default:
        log("ignoring unknown frame type \(type)")
    }
}

let reader = Thread {
    while let (type, payload) = Input.readFrame() {
        state.async { handle(type, payload) }
        if type == Frame.quit { return }
    }
    // stdin closed: the parent is gone or done with us.
    state.async { backend.quit() }
}
reader.name = "voiceio.stdin"
reader.start()

log(selfTest ? "self-test mode, waiting for config" : "waiting for config")
dispatchMain()
