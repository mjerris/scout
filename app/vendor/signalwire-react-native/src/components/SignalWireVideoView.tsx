import React from 'react';
import { RTCView } from 'react-native-webrtc';

import { useCall } from '@signalwire/react';

import type { Call } from '@signalwire/js';
import type { StyleProp, ViewStyle } from 'react-native';

export interface SignalWireVideoViewProps {
  call: Call | null | undefined;
  /** Which side of the call to render. */
  kind: 'local' | 'remote';
  objectFit?: 'contain' | 'cover';
  /** Defaults to `true` for `local`, `false` for `remote`. */
  mirror?: boolean;
  zOrder?: number;
  style?: StyleProp<ViewStyle>;
}

interface VideoCapableStream {
  toURL(): string;
  getVideoTracks?(): Array<{ id: string }>;
}

/**
 * Renders one side of a call.
 *
 * The stream URL is read on every render rather than memoized. The SDK
 * replaces tracks in place on camera switch and on server-pushed constraint
 * changes, leaving the `MediaStream` object identity unchanged — so any memo
 * keyed on the stream would freeze the view on the old track. `toURL()` is a
 * cheap string read, and `RTCView` only reacts when that string changes.
 */
export function SignalWireVideoView({
  call,
  kind,
  objectFit = 'cover',
  mirror,
  zOrder,
  style
}: SignalWireVideoViewProps): React.JSX.Element | null {
  const { localStream, remoteStream } = useCall(call);
  const stream = (kind === 'local' ? localStream : remoteStream) as VideoCapableStream | null;
  const streamURL = stream?.toURL() ?? null;

  if (!streamURL) {
    return null;
  }

  return (
    <RTCView
      testID="signalwire-video"
      streamURL={streamURL}
      objectFit={objectFit}
      mirror={mirror ?? kind === 'local'}
      zOrder={zOrder}
      style={style}
    />
  );
}
