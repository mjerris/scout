import { useCallback, useContext } from 'react';

import { SignalWireContext } from './SignalWireProvider';
import { useObservable } from './useObservable';

const NO_DEVICES: MediaDeviceInfo[] = [];

export interface UseDevicesResult {
  readonly audioInputs: MediaDeviceInfo[];
  readonly videoInputs: MediaDeviceInfo[];
  readonly selectedAudioInput: MediaDeviceInfo | null;
  readonly selectedVideoInput: MediaDeviceInfo | null;
  selectAudioInput(device: MediaDeviceInfo | null): void;
  selectVideoInput(device: MediaDeviceInfo | null): void;
  /**
   * Re-enumerates devices. React Native emits no `devicechange`, so call this
   * explicitly — on app foreground, or when opening a device picker.
   */
  refresh(): Promise<void>;
}

/**
 * Media input devices and selection.
 *
 * Audio *output* is absent by design: the SDK selects outputs through
 * `HTMLMediaElement.setSinkId`, which React Native does not have. Use
 * `useAudioRoute` from `@signalwire/react-native/audio` instead.
 */
export function useDevices(): UseDevicesResult {
  const client = useContext(SignalWireContext)?.client ?? null;

  const audioInputs = useObservable(
    client?.audioInputDevices$,
    client?.audioInputDevices ?? NO_DEVICES
  );
  const videoInputs = useObservable(
    client?.videoInputDevices$,
    client?.videoInputDevices ?? NO_DEVICES
  );
  const selectedAudioInput = useObservable(
    client?.selectedAudioInputDevice$,
    client?.selectedAudioInputDevice ?? null
  );
  const selectedVideoInput = useObservable(
    client?.selectedVideoInputDevice$,
    client?.selectedVideoInputDevice ?? null
  );

  const selectAudioInput = useCallback(
    (device: MediaDeviceInfo | null): void => client?.selectAudioInputDevice(device),
    [client]
  );

  const selectVideoInput = useCallback(
    (device: MediaDeviceInfo | null): void => client?.selectVideoInputDevice(device),
    [client]
  );

  const refresh = useCallback(
    (): Promise<void> => client?.enumerateDevices() ?? Promise.resolve(),
    [client]
  );

  return {
    audioInputs,
    videoInputs,
    selectedAudioInput,
    selectedVideoInput,
    selectAudioInput,
    selectVideoInput,
    refresh
  };
}
