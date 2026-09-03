// Compile with clang -fobjc-arc -fblocks -framework Foundation -framework UserNotifications.
// This is an offline contract test: it does not access the real notification center.
#import "../src-tauri/src/macos_notifications.m"

@interface FakeSettings : NSObject
@property UNAuthorizationStatus authorizationStatus;
@end
@implementation FakeSettings
@end

@interface FakeNotification : NSObject
@property UNNotificationRequest *request;
@end
@implementation FakeNotification
@end

@interface FakeCenter : NSObject
@property FakeSettings *settings;
@property BOOL granted;
@property NSError *authorizationError;
@property NSUInteger authorizationCalls;
@property UNAuthorizationOptions requestedOptions;
@property NSMutableArray<UNNotificationRequest *> *requests;
@end

@implementation FakeCenter
- (instancetype)init {
    if ((self = [super init])) {
        _settings = [FakeSettings new];
        _requests = [NSMutableArray new];
    }
    return self;
}
- (void)getNotificationSettingsWithCompletionHandler:(void (^)(UNNotificationSettings *))completionHandler {
    completionHandler((UNNotificationSettings *)self.settings);
}
- (void)requestAuthorizationWithOptions:(UNAuthorizationOptions)options
                     completionHandler:(void (^)(BOOL, NSError *))completionHandler {
    self.authorizationCalls++;
    self.requestedOptions = options;
    completionHandler(self.granted, self.authorizationError);
}
- (void)addNotificationRequest:(UNNotificationRequest *)request
        withCompletionHandler:(void (^)(NSError *))completionHandler {
    [self.requests addObject:request];
    completionHandler(nil);
}
@end

static FakeCenter *testSend(UNAuthorizationStatus status, BOOL granted, NSError *error, BOOL sound) {
    FakeCenter *center = [FakeCenter new];
    center.settings.authorizationStatus = status;
    center.granted = granted;
    center.authorizationError = error;
    UNMutableNotificationContent *content = [UNMutableNotificationContent new];
    content.title = @"闲鱼工作台 · 测试提醒";
    content.body = @"这是一条测试提醒，没有向买家发送消息。";
    content.sound = sound ? [UNNotificationSound defaultSound] : nil;
    sendNotification((UNUserNotificationCenter *)center, content);
    return center;
}

int main(void) {
    @autoreleasepool {
        __block NSUInteger callbacks = 0;
        for (NSNumber *sound in @[@NO, @YES]) {
            FakeNotification *notification = [FakeNotification new];
            UNMutableNotificationContent *content = [UNMutableNotificationContent new];
            content.sound = sound.boolValue ? [UNNotificationSound defaultSound] : nil;
            notification.request = [UNNotificationRequest requestWithIdentifier:@"test" content:content trigger:nil];
            [[XianyuNotificationDelegate new] userNotificationCenter:(UNUserNotificationCenter *)[FakeCenter new]
                willPresentNotification:(UNNotification *)notification
                withCompletionHandler:^(UNNotificationPresentationOptions options) {
                    callbacks++;
                    UNNotificationPresentationOptions expected = UNNotificationPresentationOptionBanner | UNNotificationPresentationOptionList;
                    if (sound.boolValue) expected |= UNNotificationPresentationOptionSound;
                    NSCAssert(options == expected, @"Foreground sound must follow content preference; no critical alerts");
                }];
        }
        NSCAssert(callbacks == 2, @"Always complete each foreground callback exactly once");

        FakeCenter *allowed = testSend(UNAuthorizationStatusAuthorized, NO, nil, NO);
        NSCAssert(allowed.requests.count == 1 && allowed.authorizationCalls == 0, @"Reuse existing permission");
        UNNotificationRequest *request = allowed.requests.firstObject;
        NSCAssert([request.content.body containsString:@"测试提醒"] && request.trigger == nil && request.content.sound == nil,
                  @"Generic, immediate and silent notification");
        sendNotification((UNUserNotificationCenter *)allowed, [UNMutableNotificationContent new]);
        NSCAssert(![request.identifier isEqualToString:allowed.requests.lastObject.identifier], @"Do not replace previous messages");

        FakeCenter *audible = testSend(UNAuthorizationStatusAuthorized, YES, nil, YES);
        NSCAssert(audible.authorizationCalls == 1 && audible.requestedOptions == (UNAuthorizationOptionAlert | UNAuthorizationOptionSound),
                  @"Register ordinary sound for upgrades from alert-only builds");
        NSCAssert(audible.requests.count == 1 && audible.requests.firstObject.content.sound != nil, @"Schedule system sound, not direct audio playback");
        NSCAssert(testSend(UNAuthorizationStatusAuthorized, NO, nil, YES).requests.count == 0, @"Respect denial during authorization refresh");
        FakeCenter *denied = testSend(UNAuthorizationStatusDenied, YES, nil, YES);
        NSCAssert(denied.requests.count == 0 && denied.authorizationCalls == 0, @"Never re-prompt or bypass denial");
        FakeCenter *first = testSend(UNAuthorizationStatusNotDetermined, YES, nil, NO);
        NSCAssert(first.requests.count == 1 && first.authorizationCalls == 1 && first.requestedOptions == UNAuthorizationOptionAlert,
                  @"Only request alert permission when needed");
        FakeCenter *firstSound = testSend(UNAuthorizationStatusNotDetermined, YES, nil, YES);
        NSCAssert(firstSound.requestedOptions == (UNAuthorizationOptionAlert | UNAuthorizationOptionSound) && firstSound.requests.count == 1,
                  @"Request sound only when enabled");
        NSCAssert(testSend(UNAuthorizationStatusNotDetermined, NO, nil, YES).requests.count == 0, @"Respect first denial");
        NSError *error = [NSError errorWithDomain:@"offline-test" code:1 userInfo:nil];
        NSCAssert(testSend(UNAuthorizationStatusNotDetermined, YES, error, YES).requests.count == 0, @"Do not submit after auth errors");
        NSCAssert(testSend(UNAuthorizationStatusProvisional, NO, nil, YES).authorizationCalls == 0, @"Do not upgrade quiet authorization");
        puts("Passed: foreground banner/list/sound toggle, callback, silent reuse, sound upgrade, denial, first authorization, auth failure, generic content, unique IDs");
    }
    return 0;
}
