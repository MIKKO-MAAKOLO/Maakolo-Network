import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:flutter/foundation.dart';

import 'assets/styles.dart';
import 'screens/auth_screen.dart';
import 'screens/main_screen.dart';
import 'utils/storage.dart';
import 'config.dart';
import 'services/localization_service.dart';
import 'services/notification_service.dart';

void main() async {
  WidgetsFlutterBinding.ensureInitialized();

  // Suppress logs in release builds
  if (kReleaseMode) {
    debugPrint = (String? message, {int? wrapWidth}) {};
  }

  // Validate config (fails early before any request)
  AppConfig.validate();

  await LocalizationService.init();
  await NotificationService().init();

  // Load session with DB timeout guard
  final storage = LocalStorage();
  Map<String, dynamic>? session;

  try {
    session = await storage.getSession().timeout(
      const Duration(seconds: 10),
      onTimeout: () {
        debugPrint("[DB] Session load timeout");
        return null;
      },
    );
  } catch (e) {
    debugPrint("[DB] Session read error: $e");
    session = null;
  }

  await SystemChrome.setPreferredOrientations([DeviceOrientation.portraitUp]);

  runApp(MaakoloApp(initialSession: session));
}

class MaakoloApp extends StatelessWidget {
  final Map<String, dynamic>? initialSession;

  const MaakoloApp({super.key, this.initialSession});

  @override
  Widget build(BuildContext context) {
    final bool hasValidSession = initialSession != null &&
        initialSession!['id'] != null &&
        initialSession!['password'] != null &&
        initialSession!['password'].toString().isNotEmpty;

    return ValueListenableBuilder<String>(
      valueListenable: LocalizationService.languageNotifier,
      builder: (context, lang, _) {
        return MaterialApp(
          title: AppConfig.appName,
          debugShowCheckedModeBanner: false,
          theme: ThemeData(
            brightness: Brightness.dark,
            scaffoldBackgroundColor: AppColors.background,
            useMaterial3: true,
          ),
          home: hasValidSession
              ? MainScreen(initialUser: initialSession!)
              : const AuthScreen(),
        );
      },
    );
  }
}
