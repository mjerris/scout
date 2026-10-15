import { parseStatus } from './audioDevices';

test('reads the status the patched InCallManager sends', () => {
  expect(
    parseStatus({
      availableAudioDeviceList: '["EARPIECE","SPEAKER_PHONE","BLUETOOTH"]',
      selectedAudioDevice: 'BLUETOOTH',
      selectedAudioDeviceName: 'KENWOOD',
    }),
  ).toEqual({ available: ['EARPIECE', 'SPEAKER_PHONE', 'BLUETOOTH'], selected: 'BLUETOOTH', selectedName: 'KENWOOD' });
  expect(parseStatus({ availableAudioDeviceList: 'nope', selectedAudioDevice: 'HDMI' })).toEqual({
    available: [],
    selected: '',
    selectedName: '',
  });
  expect(parseStatus(null).available).toEqual([]);
});
