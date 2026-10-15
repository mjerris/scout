// iOS 27 ends apps that haven't adopted the UIScene lifecycle at launch (SIGTRAP in
// _UIApplicationEvaluateRuntimeIssueForNoSceneLifecycleAdoption). Expo SDK 54's
// AppDelegate still builds its window the pre-scene way, so this adds a scene manifest
// and a SceneDelegate that puts the AppDelegate's window on the scene.
//
// With scenes, iOS sends URLs and user activities to the scene, not the app delegate, so
// the SceneDelegate forwards them: scout:// links to React Native's Linking, and a call
// started from the Phone app's recents (INStartCallIntent) to react-native-callkeep,
// which emits didReceiveStartCallAction. callkeep's class method is reached through the
// Objective-C runtime because Swift can't import RNCallKeep (react-native-callkeep#856).
const { withAppDelegate, withInfoPlist } = require('expo/config-plugins');

const MARK = '// scout: SceneDelegate';

const SCENE_DELEGATE = `
${MARK}
class SceneDelegate: UIResponder, UIWindowSceneDelegate {
  var window: UIWindow?

  func scene(
    _ scene: UIScene,
    willConnectTo session: UISceneSession,
    options connectionOptions: UIScene.ConnectionOptions
  ) {
    guard let windowScene = scene as? UIWindowScene,
          let app = UIApplication.shared.delegate as? AppDelegate,
          let appWindow = app.window else { return }
    appWindow.windowScene = windowScene
    appWindow.frame = windowScene.coordinateSpace.bounds
    window = appWindow
    appWindow.makeKeyAndVisible()
    for context in connectionOptions.urlContexts {
      _ = RCTLinkingManager.application(UIApplication.shared, open: context.url, options: [:])
    }
    for activity in connectionOptions.userActivities {
      SceneDelegate.forward(activity)
    }
  }

  func scene(_ scene: UIScene, openURLContexts URLContexts: Set<UIOpenURLContext>) {
    for context in URLContexts {
      _ = RCTLinkingManager.application(UIApplication.shared, open: context.url, options: [:])
    }
  }

  func scene(_ scene: UIScene, continue userActivity: NSUserActivity) {
    SceneDelegate.forward(userActivity)
  }

  private typealias ContinueActivity = @convention(c) (
    AnyObject, Selector, UIApplication, NSUserActivity, AnyObject?
  ) -> Bool

  static func forward(_ activity: NSUserActivity) {
    let selector = NSSelectorFromString("application:continueUserActivity:restorationHandler:")
    if let callKeep = NSClassFromString("RNCallKeep"),
       let method = class_getClassMethod(callKeep, selector) {
      let call = unsafeBitCast(method_getImplementation(method), to: ContinueActivity.self)
      if call(callKeep, selector, UIApplication.shared, activity, nil) { return }
    }
    _ = RCTLinkingManager.application(UIApplication.shared, continue: activity, restorationHandler: { _ in })
  }
}
`;

module.exports = function withSceneLifecycle(config) {
  config = withInfoPlist(config, (cfg) => {
    cfg.modResults.UIApplicationSceneManifest = {
      UIApplicationSupportsMultipleScenes: false,
      UISceneConfigurations: {
        UIWindowSceneSessionRoleApplication: [
          {
            UISceneConfigurationName: 'Default Configuration',
            UISceneDelegateClassName: '$(PRODUCT_MODULE_NAME).SceneDelegate',
          },
        ],
      },
    };
    return cfg;
  });
  return withAppDelegate(config, (cfg) => {
    if (cfg.modResults.language !== 'swift') {
      throw new Error('withSceneLifecycle: expected a Swift AppDelegate');
    }
    if (!cfg.modResults.contents.includes(MARK)) {
      cfg.modResults.contents += SCENE_DELEGATE;
    }
    return cfg;
  });
};
