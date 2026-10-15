// Two fixes for building React Native 0.81 pods with Xcode 26+, in the Podfile's
// post_install:
// - Xcode refuses pod targets whose deployment target is below its minimum (15.0), and
//   some resource-bundle targets (RNCAsyncStorage's) still say 9.0: raise every pod
//   target to the app's own minimum.
// - fmt 11.0's consteval format strings don't compile with Apple clang 17+
//   ("call to consteval function ... is not a constant expression"): build the fmt pod
//   as C++17, where fmt doesn't use consteval.
const fs = require('fs');
const path = require('path');
const { withDangerousMod } = require('expo/config-plugins');

const MARK = '# scout: pod build settings for Xcode 26+';

module.exports = function withPodBuildSettings(config, minimum = '15.1') {
  return withDangerousMod(config, [
    'ios',
    (cfg) => {
      const file = path.join(cfg.modRequest.platformProjectRoot, 'Podfile');
      let text = fs.readFileSync(file, 'utf8');
      if (!text.includes(MARK)) {
        // After react_native_post_install, which sets every pod's C++ standard itself.
        const call = /react_native_post_install\([\s\S]*?\n\s*\)\n/;
        if (!call.test(text)) throw new Error('withPodBuildSettings: no react_native_post_install in the Podfile');
        text = text.replace(
          call,
          (found) => `${found}    ${MARK}
    installer.pods_project.targets.each do |t|
      t.build_configurations.each do |c|
        if c.build_settings['IPHONEOS_DEPLOYMENT_TARGET'].to_f < ${minimum}
          c.build_settings['IPHONEOS_DEPLOYMENT_TARGET'] = '${minimum}'
        end
        c.build_settings['CLANG_CXX_LANGUAGE_STANDARD'] = 'c++17' if t.name == 'fmt'
      end
    end
`,
        );
        fs.writeFileSync(file, text);
      }
      return cfg;
    },
  ]);
};
