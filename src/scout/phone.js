// The /phone test page: pair this browser with Scout, then call Scout's SignalWire agent
// with signed userVariables (phone_sign.js). Uses @signalwire/js v4 (window.SignalWire).
(function () {
  "use strict";
  const KEY = "scout.phone.pairing";
  const $ = (id) => document.getElementById(id);
  let client = null;
  let call = null;
  let busy = false;
  let attempt = 0; // bumped by Hang up, so a dial still in progress is dropped when it lands

  function loadPairing() {
    try {
      const p = JSON.parse(localStorage.getItem(KEY) || "null");
      return p && p.device && p.secret && p.address ? p : null;
    } catch (e) {
      return null;
    }
  }

  function setState(text) {
    $("state").textContent = text;
  }

  function showError(err) {
    $("error").textContent = err ? String((err && err.message) || err) : "";
    if (err) console.error(err);
  }

  function render() {
    const p = loadPairing();
    $("unpaired").hidden = !!p;
    $("paired").hidden = !p;
    if (p) {
      $("device").textContent = p.device;
      $("address").textContent = p.address;
    }
    $("call").disabled = !p || busy || !!call;
    $("hangup").disabled = !call && !busy;
    $("forget").disabled = busy || !!call;
  }

  async function postJSON(url, body) {
    const r = await fetch(url, {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body || {}),
    });
    let data = null;
    try {
      data = await r.json();
    } catch (e) {
      data = null;
    }
    if (!r.ok) {
      const why = (data && data.error) || (r.status === 401 ? "Not signed in: open this page with ?token=..." : "");
      throw new Error(`${url} failed (HTTP ${r.status})${why ? ": " + why : ""}`);
    }
    return data;
  }

  async function pair() {
    showError("");
    const name = $("name").value.trim();
    if (!name) {
      showError("Give this browser a name first.");
      return;
    }
    $("pair").disabled = true;
    try {
      const p = await postJSON("/api/phone/pair", { name });
      localStorage.setItem(KEY, JSON.stringify({ device: p.device, secret: p.secret, address: p.address }));
    } catch (e) {
      showError(e);
    } finally {
      $("pair").disabled = false;
      render();
    }
  }

  function forget() {
    localStorage.removeItem(KEY);
    showError("");
    render();
  }

  async function teardown() {
    const c = client;
    call = null;
    client = null;
    $("remote").srcObject = null;
    if (c) {
      try {
        await c.disconnect();
      } catch (e) {
        console.warn("disconnect failed", e);
      }
    }
    render();
  }

  async function startCall() {
    const p = loadPairing();
    if (!p || call || busy) return;
    if (!window.SignalWire || !window.SignalWire.SignalWire) {
      showError("The SignalWire SDK didn't load (check the network or content blockers).");
      return;
    }
    showError("");
    busy = true;
    const mine = ++attempt;
    const stale = () => mine !== attempt;
    render();
    const audio = $("remote");
    audio.play().catch(() => {}); // unlock playback inside the click (iOS)
    try {
      setState("getting a token");
      const { token } = await postJSON("/api/phone/token");
      if (stale()) return;
      setState("connecting");
      const SW = window.SignalWire;
      client = new SW.SignalWire(new SW.StaticCredentialProvider({ token }), {
        skipRegister: true, // outbound only: no need to receive calls
        skipDeviceMonitoring: true,
      });
      client.errors$.subscribe((e) => showError(e));
      const userVariables = await ScoutSign.callVariables(p);
      if (stale()) return;
      setState("dialing");
      const placed = await client.dial(p.address, {
        audio: true,
        video: false,
        receiveAudio: true,
        receiveVideo: false,
        userVariables,
      });
      if (stale()) {
        placed.hangup().catch(() => {});
        return;
      }
      call = placed;
      busy = false;
      render();
      call.remoteStream$.subscribe((stream) => {
        audio.srcObject = stream;
        audio.play().catch((e) => showError(`Tap Call again if you hear nothing: ${e.message || e}`));
      });
      call.errors$.subscribe((e) => showError((e && e.error) || e));
      call.status$.subscribe((status) => {
        setState(status);
        if ((status === "disconnected" || status === "failed" || status === "destroyed") && call === placed) teardown();
      });
    } catch (e) {
      if (stale()) return;
      busy = false;
      setState("failed");
      showError(e);
      await teardown();
    }
  }

  async function hangup() {
    attempt++;
    busy = false;
    const c = call;
    if (c) {
      setState("hanging up");
      try {
        await c.hangup();
      } catch (e) {
        showError(e);
      }
    }
    setState("idle");
    await teardown();
  }

  $("pair").addEventListener("click", pair);
  $("name").addEventListener("keydown", (e) => {
    if (e.key === "Enter") pair();
  });
  $("forget").addEventListener("click", forget);
  $("call").addEventListener("click", startCall);
  $("hangup").addEventListener("click", hangup);
  window.addEventListener("pagehide", () => {
    if (call) call.hangup().catch(() => {});
  });
  render();
})();
