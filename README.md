# 🌐 Maakolo Network

Secure VPN infrastructure with dynamic user routing.
Team pet project (8 people, ~8 months), currently **on hold**.

## Tech Stack
- **Backend:** Python 3, Flask, PostgreSQL, Redis
- **Bot:** Python 3, aiogram 3 (async), aiosqlite
- **Core:** Xray-core / Sing-box
- **Protocols:** VLESS (Reality), Hysteria 2 (QUIC)
- **Payments:** Platega (card/SBP), Plisio (crypto), Telegram Stars — HMAC-verified webhooks, idempotent grants, underpayment fraud checks

## Security Features (My Focus)
- **Authentication:** salted password hashing, optional 2FA, timed session tokens
- **API Protection:** rate limiting, request validation, anti-abuse measures
- **Payments:** secure webhook verification, idempotent processing, fraud detection
- **Infrastructure:** VPS hardening (SSH key-only, firewall, fail2ban)
- **Logging:** secret masking before disk write

## System Components
1. **Flask API:** accounts, subscriptions, payments, proxy management
2. **PostgreSQL:** user data, subscriptions, payment registry
3. **Android APK:** native client for Android users (ID + password sign-in, built-in protocol client)
4. **Telegram Bot:** client for iOS/PC and fallback (EN/RU/FI), setup guides, support tickets, APK delivery
5. **Expiration Watchdog:** automatic cleanup of expired subscriptions

## Status
On hold. Code preserved as-is for reference.

---
<details>
<summary>🇷🇺 По-русски</summary>

# 🌐 Maakolo Network

Защищённая VPN-инфраструктура с динамической маршрутизацией.
Командный проект (8 человек, ~8 месяцев), временно **заморожен**.

## Технологии
- **Backend:** Python 3, Flask, PostgreSQL, Redis
- **Бот:** Python 3, aiogram 3 (асинхронный), aiosqlite
- **Ядро:** Xray-core / Sing-box
- **Протоколы:** VLESS (Reality), Hysteria 2 (QUIC)
- **Платежи:** Platega (карта/СБП), Plisio (крипта), Telegram Stars — вебхуки с HMAC-проверкой, идемпотентная активация, проверка недоплаты

## Безопасность (моя зона ответственности)
- **Аутентификация:** хэширование паролей с солью, опциональная 2FA, сессионные токены с временем жизни
- **Защита API:** rate limiting, валидация запросов, защита от злоупотреблений
- **Платежи:** безопасная проверка вебхуков, идемпотентная обработка, обнаружение мошенничества
- **Инфраструктура:** hardening сервера (SSH только по ключам, firewall, fail2ban)
- **Логирование:** маскирование секретов перед записью на диск

## Компоненты системы
1. **Flask API:** аккаунты, подписки, платежи, управление прокси
2. **PostgreSQL:** данные пользователей, подписки, реестр платежей
3. **Android APK:** нативный клиент для Android (вход по ID + паролю, встроенный клиент протокола)
4. **Telegram-бот:** клиент для iOS/PC и fallback (EN/RU/FI), гайды, тикеты в поддержку, выдача APK
5. **Watchdog просрочек:** автоматическая очистка просроченных подписок

## Статус
На паузе. Код сохранён как есть для справки.

</details>
