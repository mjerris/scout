import { StatusBar } from 'expo-status-bar';
import { useCallback, useEffect, useState } from 'react';
import { ActivityIndicator, Alert, Linking, ScrollView, StyleSheet, Text, View } from 'react-native';
import { SafeAreaProvider, SafeAreaView } from 'react-native-safe-area-context';

import { CallPanel } from './CallPanel';

import { clearPairing, loadPairing, parsePairLink } from './pairing';
import type { PairLink, Pairing } from './pairing';
import { PairScreen } from './PairScreen';
import { testFlags } from './testFlags';
import { useTheme } from './theme';
import { Button, Muted, Row, Section } from './ui';

export default function App() {
  return (
    <SafeAreaProvider>
      <Main />
    </SafeAreaProvider>
  );
}

function Main() {
  const t = useTheme();
  const [pairing, setPairing] = useState<Pairing | null | undefined>(undefined);
  const [link, setLink] = useState<PairLink | null>(null);
  const [loadError, setLoadError] = useState('');

  useEffect(() => {
    loadPairing()
      .then(setPairing)
      .catch((e: unknown) => {
        setLoadError(`Couldn't read the Keychain: ${String((e as Error)?.message ?? e)}`);
        setPairing(null);
      });
  }, []);

  // scout://pair?server=...&code=... (from a QR code, through the Camera app).
  useEffect(() => {
    const take = (url: string | null) => {
      const l = url ? parsePairLink(url) : null;
      if (l) setLink(l);
    };
    Linking.getInitialURL().then(take, () => {});
    if (testFlags.link) take(testFlags.link);
    const sub = Linking.addEventListener('url', (e) => take(e.url));
    return () => sub.remove();
  }, []);

  const forget = useCallback(() => {
    Alert.alert('Forget this pairing?', 'This phone stops calling Scout until it is paired again.', [
      { text: 'Cancel', style: 'cancel' },
      {
        text: 'Forget',
        style: 'destructive',
        onPress: () => {
          clearPairing().finally(() => setPairing(null));
        },
      },
    ]);
  }, []);

  return (
    <SafeAreaView style={[styles.root, { backgroundColor: t.bg }]}>
      <StatusBar style="auto" />
      <ScrollView contentContainerStyle={styles.main} keyboardShouldPersistTaps="handled">
        <Text style={[styles.h1, { color: t.ink }]}>Scout</Text>
        {loadError ? <Muted>{loadError}</Muted> : null}
        {pairing === undefined ? (
          <View style={styles.center}>
            <ActivityIndicator />
          </View>
        ) : pairing === null ? (
          <PairScreen link={link} onPaired={setPairing} onDismissLink={() => setLink(null)} autoPair={testFlags.pair} />
        ) : (
          <Paired pairing={pairing} onForget={forget} />
        )}
      </ScrollView>
    </SafeAreaView>
  );
}

function Paired({ pairing, onForget }: { pairing: Pairing; onForget: () => void }) {
  const t = useTheme();
  return (
    <>
    <CallPanel pairing={pairing} />
    <Section title="Paired">
      <Text style={{ color: t.ink }} testID="paired">
        {pairing.name} ({pairing.device}), calling {pairing.address}
      </Text>
      <Muted>Scout at {pairing.server}</Muted>
      <Row>
        <Button testID="forget" title="Forget pairing" onPress={onForget} />
      </Row>
      <Muted>
        Forgetting here only clears this phone. To revoke it in Scout: scoutctl phone forget {pairing.device}
      </Muted>
    </Section>
    </>
  );
}

const styles = StyleSheet.create({
  root: { flex: 1 },
  main: { padding: 16, gap: 16 },
  h1: { fontSize: 28, fontWeight: '700', marginTop: 4 },
  center: { paddingVertical: 40, alignItems: 'center' },
});
