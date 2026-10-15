// What the phone is actually playing a call through, from react-native-incall-manager's
// iOS routes (our patch, patches/react-native-incall-manager+4.2.2.patch): the library's
// route state is only what was asked for, and iOS decides (a car or headset connecting
// takes the call over).
import { useEffect, useState } from 'react';
import { NativeEventEmitter, NativeModules, Platform } from 'react-native';

export type DeviceName = 'EARPIECE' | 'SPEAKER_PHONE' | 'WIRED_HEADSET' | 'BLUETOOTH';

export interface AudioDevices {
  available: DeviceName[];
  selected: DeviceName | '';
  /** The output's own name, e.g. the car's or the headset's. */
  selectedName: string;
}

const NONE: AudioDevices = { available: [], selected: '', selectedName: '' };
const NAMES: readonly string[] = ['EARPIECE', 'SPEAKER_PHONE', 'WIRED_HEADSET', 'BLUETOOTH'];

export function parseStatus(raw: unknown): AudioDevices {
  if (!raw || typeof raw !== 'object') return NONE;
  const o = raw as Record<string, unknown>;
  let list: unknown = o.availableAudioDeviceList;
  if (typeof list === 'string') {
    try {
      list = JSON.parse(list);
    } catch {
      list = [];
    }
  }
  const available = Array.isArray(list) ? (list.filter((d) => NAMES.includes(d as string)) as DeviceName[]) : [];
  const sel = typeof o.selectedAudioDevice === 'string' && NAMES.includes(o.selectedAudioDevice) ? o.selectedAudioDevice : '';
  return {
    available,
    selected: sel as DeviceName | '',
    selectedName: typeof o.selectedAudioDeviceName === 'string' ? o.selectedAudioDeviceName : '',
  };
}

type Native = { getAudioDeviceStatus?: () => Promise<unknown> };

/** The current audio devices while `active` (during a call). */
export function useAudioDevices(active: boolean): AudioDevices {
  const [devices, setDevices] = useState<AudioDevices>(NONE);
  useEffect(() => {
    const native = NativeModules.InCallManager as Native | undefined;
    if (!active || Platform.OS !== 'ios' || !native) return;
    let live = true;
    const emitter = new NativeEventEmitter(NativeModules.InCallManager);
    const sub = emitter.addListener('onAudioDeviceChanged', (e: unknown) => live && setDevices(parseStatus(e)));
    native
      .getAudioDeviceStatus?.()
      .then((s) => live && setDevices(parseStatus(s)))
      .catch(() => {});
    return () => {
      live = false;
      sub.remove();
    };
  }, [active]);
  return devices;
}
