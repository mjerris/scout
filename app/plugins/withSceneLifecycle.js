// iOS 27 ends apps that haven't adopted the UIScene lifecycle at launch (SIGTRAP in
// _UIApplicationEvaluateRuntimeIssueForNoSceneLifecycleAdoption). Expo SDK 54's
// AppDelegate still builds its window the pre-scene way, so this adds a scene manifest
// and a SceneDelegate that puts the AppDelegate's window on the scene.
//
// With scenes, iOS sends URLs and user activities to the scene, not the app delegate, so
// the SceneDelegate forwards them (on a cold start, once the first screen is up): scout:// links to React Native's Linking, and a call
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
    // A cold start: React Native's Linking reads its initial URL from the launch options,
    // which carry nothing under scenes, and nothing listens yet. Hand the link (or the
    // recents call) over once the first screen is up.
    let urls = connectionOptions.urlContexts.map { $0.url }
    let activities = Array(connectionOptions.userActivities)
    if urls.isEmpty && activities.isEmpty { return }
    var token: NSObjectProtocol?
    token = NotificationCenter.default.addObserver(
      forName: NSNotification.Name("RCTContentDidAppearNotification"), object: nil, queue: .main
    ) { _ in
      if let t = token { NotificationCenter.default.removeObserver(t) }
      DispatchQueue.main.asyncAfter(deadline: .now() + 0.5) {
        for url in urls {
          _ = RCTLinkingManager.application(UIApplication.shared, open: url, options: [:])
        }
        for activity in activities { SceneDelegate.forward(activity) }
      }
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
