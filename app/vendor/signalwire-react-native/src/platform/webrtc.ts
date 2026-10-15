import {
  mediaDevices as rnMediaDevices,
  RTCPeerConnection as RNPeerConnection,
  RTCRtpSender as RNRtpSender
} from 'react-native-webrtc';

import { logger } from '@signalwire/react';
import { assertPeerModule } from './peers';

import type { WebRTCApiProvider, WebRTCMediaDevices } from '@signalwire/js';

/**
 * `RTCRtpSender.setStreams()` is a browser API that react-native-webrtc does
 * not implement. The SDK calls it on its inbound-answer path — reusing the
 * transceivers `setRemoteDescription` created from the offer — so answering
 * ANY inbound call on React Native threw "undefined is not a function" after
 * the user had already accepted on the native UI. Outbound calls never hit it:
 * they take the `addTransceiver` branch.
 *
 * A no-op is the honest shim. `setStreams` only re-associates the sender with
 * media streams for `a=msid` grouping in the SDP, and react-native-webrtc
 * offers no API to change that association after the fact. For single-stream
 * calls — every call this package makes — the association the answer already
 * carries is correct.
 *
 * Only installed when missing, so a react-native-webrtc release that implements
 * it wins automatically.
 */
function installSenderSetStreamsShim(): void {
  const proto = (RNRtpSender as unknown as { prototype?: Record<string, unknown> })?.prototype;
  if (proto && typeof proto.setStreams !== 'function') {
    proto.setStreams = function setStreams(): void {
      logger.debug('RTCRtpSender.setStreams is a no-op on React Native (shimmed)');
    };
  }
}

let warnedAboutDeviceEvents = false;

function warnOnceAboutDeviceEvents(): void {
  if (warnedAboutDeviceEvents) {
    return;
  }
  warnedAboutDeviceEvents = true;
  logger.debug(
    'React Native emits no "devicechange" event. Device listeners are no-ops; ' +
      'call enumerateDevices() manually when you need a refreshed list.'
  );
}

/**
 * Builds the SDK's WebRTC provider from `react-native-webrtc`.
 *
 * `getDisplayMedia` is intentionally absent: the SDK probes for it to decide
 * whether screen share is supported, and React Native cannot provide it.
 */
export function createWebRTCApiProvider(): WebRTCApiProvider {
  assertPeerModule(
    rnMediaDevices as object | undefined,
    'react-native-webrtc',
    'WebRTC support',
    'getUserMedia'
  );

  installSenderSetStreamsShim();

  const mediaDevices: WebRTCMediaDevices = {
    getUserMedia: (constraints) =>
      rnMediaDevices.getUserMedia(constraints as never) as unknown as Promise<MediaStream>,
    enumerateDevices: () =>
      rnMediaDevices.enumerateDevices() as unknown as Promise<MediaDeviceInfo[]>,
    addEventListener: () => warnOnceAboutDeviceEvents(),
    removeEventListener: () => warnOnceAboutDeviceEvents()
  };

  return {
    RTCPeerConnection: RNPeerConnection as unknown as typeof RTCPeerConnection,
    mediaDevices
  };
}
