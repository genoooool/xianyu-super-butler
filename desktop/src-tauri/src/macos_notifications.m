#import <Foundation/Foundation.h>
#import <UserNotifications/UserNotifications.h>
#import <os/log.h>

@interface XianyuNotificationDelegate : NSObject <UNUserNotificationCenterDelegate>
@end

@implementation XianyuNotificationDelegate
- (void)userNotificationCenter:(UNUserNotificationCenter *)center
      willPresentNotification:(UNNotification *)notification
        withCompletionHandler:(void (^)(UNNotificationPresentationOptions))completionHandler {
    // Foreground apps are silent unless they explicitly request presentation.
    // These ordinary options still respect system authorization and Focus.
    os_log_info(OS_LOG_DEFAULT, "Xianyu notification: foreground banner requested");
    completionHandler(UNNotificationPresentationOptionBanner | UNNotificationPresentationOptionList);
}
@end

static XianyuNotificationDelegate *notificationDelegate;
static UNUserNotificationCenter *notificationCenter;

void xianyu_notifications_initialize(void) {
    static dispatch_once_t onceToken;
    dispatch_once(&onceToken, ^{
        // A raw `cargo run` executable has no app identity. UN would raise an
        // Objective-C exception; keep that development mode usable without alerts.
        if ([NSBundle mainBundle].bundleIdentifier.length == 0) {
            os_log_error(OS_LOG_DEFAULT, "Xianyu notification: a signed app bundle is required");
            return;
        }
        // UNUserNotificationCenter keeps a weak reference to its delegate.
        notificationDelegate = [XianyuNotificationDelegate new];
        notificationCenter = [UNUserNotificationCenter currentNotificationCenter];
        notificationCenter.delegate = notificationDelegate;
    });
}

static void enqueueNotification(UNUserNotificationCenter *center, UNMutableNotificationContent *content) {
    UNNotificationRequest *request = [UNNotificationRequest
        requestWithIdentifier:[NSUUID UUID].UUIDString content:content trigger:nil];
    [center addNotificationRequest:request withCompletionHandler:^(NSError *error) {
        if (error) {
            os_log_error(OS_LOG_DEFAULT, "Xianyu notification: scheduling failed (%{public}@, %ld)",
                         error.domain, (long)error.code);
        } else {
            // Accepted by macOS does not imply a banner was visible.
            os_log_info(OS_LOG_DEFAULT, "Xianyu notification: request %{public}@ accepted",
                        request.identifier);
        }
    }];
}

static void sendNotification(UNUserNotificationCenter *center, UNMutableNotificationContent *content) {
    [center getNotificationSettingsWithCompletionHandler:^(UNNotificationSettings *settings) {
        if (settings.authorizationStatus == UNAuthorizationStatusNotDetermined) {
            // Ask only on a message/test, never on startup. No sound, badge or
            // critical/time-sensitive authorization; never override a denial.
            [center requestAuthorizationWithOptions:UNAuthorizationOptionAlert
                                  completionHandler:^(BOOL granted, NSError *error) {
                if (error) {
                    os_log_error(OS_LOG_DEFAULT, "Xianyu notification: authorization failed (%{public}@, %ld)",
                                 error.domain, (long)error.code);
                } else if (granted) {
                    enqueueNotification(center, content);
                }
            }];
        } else if (settings.authorizationStatus == UNAuthorizationStatusAuthorized ||
                   settings.authorizationStatus == UNAuthorizationStatusProvisional) {
            enqueueNotification(center, content);
        } else {
            os_log_info(OS_LOG_DEFAULT, "Xianyu notification: disabled in system settings");
        }
    }];
}

void xianyu_notifications_show(const char *title, const char *body) {
    @autoreleasepool {
        UNMutableNotificationContent *content = [UNMutableNotificationContent new];
        content.title = [NSString stringWithUTF8String:title];
        content.body = [NSString stringWithUTF8String:body];
        // Only generic message counts arrive here, never buyer names or text.
        sendNotification(notificationCenter, content);
    }
}
