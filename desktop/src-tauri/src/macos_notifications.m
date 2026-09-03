#import <Foundation/Foundation.h>
#import <UserNotifications/UserNotifications.h>
#import <os/log.h>
#include <stdbool.h>

@interface XianyuNotificationDelegate : NSObject <UNUserNotificationCenterDelegate>
@end

@implementation XianyuNotificationDelegate
- (void)userNotificationCenter:(UNUserNotificationCenter *)center
      willPresentNotification:(UNNotification *)notification
        withCompletionHandler:(void (^)(UNNotificationPresentationOptions))completionHandler {
    // Foreground apps are silent unless they explicitly request presentation.
    // These ordinary options still respect system authorization and Focus.
    os_log_info(OS_LOG_DEFAULT, "Xianyu notification: foreground banner requested");
    UNNotificationPresentationOptions options = UNNotificationPresentationOptionBanner | UNNotificationPresentationOptionList;
    if (notification.request.content.sound != nil) {
        options |= UNNotificationPresentationOptionSound;
    }
    completionHandler(options);
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
            os_log_info(OS_LOG_DEFAULT, "Xianyu notification: request %{public}@ accepted (sound requested: %{public}@)",
                        request.identifier, content.sound ? @"yes" : @"no");
        }
    }];
}

static void sendNotification(UNUserNotificationCenter *center, UNMutableNotificationContent *content) {
    [center getNotificationSettingsWithCompletionHandler:^(UNNotificationSettings *settings) {
        if (settings.authorizationStatus == UNAuthorizationStatusNotDetermined ||
            (settings.authorizationStatus == UNAuthorizationStatusAuthorized && content.sound != nil)) {
            // Ask only on a message/test. Include sound when the user enables it,
            // including upgrades from our old alert-only build. Repeated requests
            // do not re-prompt or override user settings. Never request critical alerts.
            UNAuthorizationOptions options = UNAuthorizationOptionAlert;
            if (content.sound != nil) options |= UNAuthorizationOptionSound;
            [center requestAuthorizationWithOptions:options
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

void xianyu_notifications_show(const char *title, const char *body, bool sound) {
    @autoreleasepool {
        UNMutableNotificationContent *content = [UNMutableNotificationContent new];
        content.title = [NSString stringWithUTF8String:title];
        content.body = [NSString stringWithUTF8String:body];
        content.sound = sound ? [UNNotificationSound defaultSound] : nil;
        // Only generic message counts arrive here, never buyer names or text.
        sendNotification(notificationCenter, content);
    }
}
