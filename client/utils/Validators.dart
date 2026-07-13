import '../config.dart';
import '../services/l10n.dart'; 

abstract class Validators {
  Validators._();

  /// Validates user ID (exactly 16 digits)
  static (bool, String) validateUserId(String? userId) {
    final uid = (userId ?? "").trim().replaceAll(RegExp(r'\s+'), "");

    if (uid.isEmpty) {
      return (false, t('id_empty'));
    }

    if (!RegExp(r'^\d{16}$').hasMatch(uid)) {
      return (false, t('id_format'));
    }

    return (true, "");
  }

  
  static (bool, String) validatePassword(String? password, {int? minLength}) {
    final minLen = minLength ?? AppConfig.minPasswordLength;
    final pwd = password ?? "";

    if (pwd.isEmpty) {
      return (false, t('pass_empty'));
    }

    if (pwd.length < minLen) {
      return (false, t('pass_short', n: '$minLen'));
    }

    // Reject Cyrillic: KDF key normalization issues
    if (RegExp(r'[а-яА-ЯёЁ]').hasMatch(pwd)) {
      return (false, t('pass_cyrillic'));
    }

    if (!pwd.contains(RegExp(r'\d'))) {
      return (false, t('pass_digit'));
    }

    if (!pwd.contains(RegExp(r'[a-zA-Z]'))) {
      return (false, t('pass_letter'));
    }

    
    if (!pwd.contains(RegExp(r'[!@#$%^&*(),.?":{}|<>]'))) {
      return (false, t('pass_special'));
    }

    return (true, "");
  }

  static (bool, String) validateServerId(String? serverId) {
    if (serverId == null || serverId.trim().length < 2) {
      return (false, t('select_server'));
    }
    return (true, "");
  }
}
