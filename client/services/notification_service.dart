import 'package:flutter_local_notifications/flutter_local_notifications.dart';
import 'package:timezone/timezone.dart' as tz;
import 'package:timezone/data/latest.dart' as tz_data;
import 'l10n.dart';

class NotificationService {
  static final NotificationService _instance = NotificationService._internal();
  factory NotificationService() => _instance;
  NotificationService._internal();

  final FlutterLocalNotificationsPlugin _notifications = FlutterLocalNotificationsPlugin();

  Future<void> init() async {
    tz_data.initializeTimeZones();

    const AndroidInitializationSettings androidSettings =
    AndroidInitializationSettings('@mipmap/ic_launcher');

    const DarwinInitializationSettings iosSettings = DarwinInitializationSettings(
      requestAlertPermission: true,
      requestBadgePermission: true,
      requestSoundPermission: true,
    );

    const InitializationSettings settings = InitializationSettings(
      android: androidSettings,
      iOS: iosSettings,
    );

    await _notifications.initialize(settings);
  }

  /// Schedules subscription expiry notifications.
  Future<void> scheduleSubscriptionNotifications({
    int? baseExpiryMs,
    int? stealthExpiryMs,
  }) async {
    await _notifications.cancelAll();

    final now = DateTime.now();

    if (baseExpiryMs != null && baseExpiryMs > 0) {
      await _scheduleForType(baseExpiryMs, 0, now);
    }

    if (stealthExpiryMs != null && stealthExpiryMs > 0) {
      await _scheduleForType(stealthExpiryMs, 10, now);
    }
  }

  Future<void> _scheduleForType(int expiryMs, int idOffset, DateTime now) async {
    final expiryDate = DateTime.fromMillisecondsSinceEpoch(expiryMs);
    final difference = expiryDate.difference(now);

    if (difference.inDays < -1) return;

    for (int daysLeft = 3; daysLeft >= 0; daysLeft--) {
      final scheduledTime = expiryDate.subtract(Duration(days: daysLeft));

      if (scheduledTime.isBefore(now)) continue;

      final String title = t('notif_sub_title');
      final String body = daysLeft == 0
          ? t('notif_sub_expired')
          : t('notif_sub_days_left', n: daysLeft.toString());


      await _notifications.zonedSchedule(
        idOffset + daysLeft,
        title,
        body,
        tz.TZDateTime.from(scheduledTime, tz.local),
        const NotificationDetails(
          android: AndroidNotificationDetails(
            'subscription_channel',
            'Subscription Alerts',
            channelDescription: 'Notifications about subscription expiration',
            importance: Importance.max,
            priority: Priority.high,
          ),
          iOS: DarwinNotificationDetails(),
        ),
        androidScheduleMode: AndroidScheduleMode.exactAllowWhileIdle,
        uiLocalNotificationDateInterpretation:
        UILocalNotificationDateInterpretation.absoluteTime,
      );
    }
  }
}