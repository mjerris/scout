// CoreAudio device lookup: names, defaults, and pointing an IO unit at a device.

import AudioToolbox
import CoreAudio
import Foundation

struct AudioDevice {
    let id: AudioDeviceID
    let name: String
    let inputs: Bool
    let outputs: Bool
}

private func address(_ selector: AudioObjectPropertySelector,
                     _ scope: AudioObjectPropertyScope = kAudioObjectPropertyScopeGlobal)
    -> AudioObjectPropertyAddress
{
    AudioObjectPropertyAddress(mSelector: selector, mScope: scope, mElement: kAudioObjectPropertyElementMain)
}

enum Devices {
    static let system = AudioObjectID(kAudioObjectSystemObject)

    static func all() -> [AudioDevice] {
        var addr = address(kAudioHardwarePropertyDevices)
        var size: UInt32 = 0
        guard AudioObjectGetPropertyDataSize(system, &addr, 0, nil, &size) == noErr, size > 0 else { return [] }
        var ids = [AudioDeviceID](repeating: 0, count: Int(size) / MemoryLayout<AudioDeviceID>.size)
        guard AudioObjectGetPropertyData(system, &addr, 0, nil, &size, &ids) == noErr else { return [] }
        return ids.map {
            AudioDevice(id: $0, name: name($0), inputs: hasStreams($0, kAudioObjectPropertyScopeInput),
                        outputs: hasStreams($0, kAudioObjectPropertyScopeOutput))
        }.filter { !isPrivateAggregate($0.name) }
    }

    /// Voice processing creates private aggregate devices
    /// ("CADefaultDeviceAggregate-<pid>-0", "VPAUAggregateAudioDevice-0x..."), one
    /// set per process using it; they are never what a user means by a device name.
    static func isPrivateAggregate(_ name: String) -> Bool {
        name.hasPrefix("CADefaultDeviceAggregate") || name.hasPrefix("VPAUAggregateAudioDevice")
    }

    static func name(_ id: AudioDeviceID) -> String {
        var addr = address(kAudioObjectPropertyName)
        var value: Unmanaged<CFString>?
        var size = UInt32(MemoryLayout<Unmanaged<CFString>?>.size)
        guard AudioObjectGetPropertyData(id, &addr, 0, nil, &size, &value) == noErr, let value else {
            return "device \(id)"
        }
        return value.takeRetainedValue() as String
    }

    static func hasStreams(_ id: AudioDeviceID, _ scope: AudioObjectPropertyScope) -> Bool {
        var addr = address(kAudioDevicePropertyStreams, scope)
        var size: UInt32 = 0
        return AudioObjectGetPropertyDataSize(id, &addr, 0, nil, &size) == noErr && size > 0
    }

    static func defaultDevice(input: Bool) -> AudioDeviceID? {
        var addr = address(input ? kAudioHardwarePropertyDefaultInputDevice
                                 : kAudioHardwarePropertyDefaultOutputDevice)
        var id = AudioDeviceID(0)
        var size = UInt32(MemoryLayout<AudioDeviceID>.size)
        guard AudioObjectGetPropertyData(system, &addr, 0, nil, &size, &id) == noErr, id != 0 else { return nil }
        return id
    }

    /// First device whose name contains `substring` (case-insensitive) and has the right direction.
    static func find(_ substring: String, input: Bool) -> AudioDevice? {
        all().first {
            (input ? $0.inputs : $0.outputs) && $0.name.range(of: substring, options: .caseInsensitive) != nil
        }
    }

    /// Point an IO audio unit at a device. On AUVoiceProcessingIO element 1 is
    /// the input side and element 0 the output side.
    static func setCurrent(_ unit: AudioUnit, device: AudioDeviceID, element: AudioUnitElement) throws {
        var id = device
        let status = AudioUnitSetProperty(unit, kAudioOutputUnitProperty_CurrentDevice, kAudioUnitScope_Global,
                                          element, &id, UInt32(MemoryLayout<AudioDeviceID>.size))
        if status != noErr {
            throw VoiceIOError("cannot select device \(name(device)) (OSStatus \(status))")
        }
    }

    static func current(_ unit: AudioUnit, element: AudioUnitElement) -> AudioDeviceID? {
        var id = AudioDeviceID(0)
        var size = UInt32(MemoryLayout<AudioDeviceID>.size)
        let status = AudioUnitGetProperty(unit, kAudioOutputUnitProperty_CurrentDevice, kAudioUnitScope_Global,
                                          element, &id, &size)
        return status == noErr && id != 0 ? id : nil
    }

    /// Call `handler` on `queue` whenever the device list or a default device changes.
    static func watch(queue: DispatchQueue, _ handler: @escaping (String) -> Void) {
        let selectors: [(AudioObjectPropertySelector, String)] = [
            (kAudioHardwarePropertyDevices, "device list changed"),
            (kAudioHardwarePropertyDefaultInputDevice, "default input changed"),
            (kAudioHardwarePropertyDefaultOutputDevice, "default output changed"),
        ]
        for (selector, reason) in selectors {
            var addr = address(selector)
            let status = AudioObjectAddPropertyListenerBlock(system, &addr, queue) { _, _ in handler(reason) }
            if status != noErr {
                log("cannot watch \(reason) (OSStatus \(status))")
            }
        }
    }
}
