// Siri (docs/siri-design.md): copies plugins/ScoutIntents.swift, the App Intents and App
// Shortcuts ("Ask Scout", "Scout briefing", ...), into the generated iOS project and adds
// it to the app target. App Intents need no entitlement or Info.plist key; Xcode extracts
// their metadata at build time.
const fs = require('fs');
const path = require('path');
const { IOSConfig, withDangerousMod, withXcodeProject } = require('expo/config-plugins');

const FILE = 'ScoutIntents.swift';

module.exports = function withSiriIntents(config) {
  config = withDangerousMod(config, [
    'ios',
    (cfg) => {
      const name = IOSConfig.XcodeUtils.getProjectName(cfg.modRequest.projectRoot);
      fs.copyFileSync(path.join(cfg.modRequest.projectRoot, 'plugins', FILE), path.join(cfg.modRequest.platformProjectRoot, name, FILE));
      return cfg;
    },
  ]);
  return withXcodeProject(config, (cfg) => {
    const name = IOSConfig.XcodeUtils.getProjectName(cfg.modRequest.projectRoot);
    const filepath = `${name}/${FILE}`;
    if (!cfg.modResults.hasFile(filepath)) {
      IOSConfig.XcodeUtils.addBuildSourceFileToGroup({ filepath, groupName: name, project: cfg.modResults });
    }
    return cfg;
  });
};
