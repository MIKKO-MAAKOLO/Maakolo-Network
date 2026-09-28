# 🌐 Maakolo Network

Secure VPN infrastructure with dynamic user routing.
Team pet project (8 people), currently **on hold**.

## Tech Stack
- **Backend:** Python 3, Flask, PostgreSQL, Redis
- **Bot:** Python 3, aiogram 3 (async), aiosqlite
- **Core:** Xray-core / Sing-box
- **Protocols:** VLESS (Reality), Hysteria 2 (QUIC)

## Security Features (My Focus)
- **Authentication:** salted password hashing, optional 2FA, timed session tokens
- **API Protection:** rate limiting, request validation, anti-abuse measures
- **Payments:** secure webhook verification, idempotent processing, fraud detection
- **Infrastructure:** VPS hardening (SSH key-only, firewall, fail2ban)
- **Logging:** secret masking before disk write

## System Components
1. **Flask API:** accounts, subscriptions, payments, proxy management
2. **PostgreSQL:** user data, subscriptions, payment registry
3. **Telegram Bot:** user frontend (EN/RU/FI), setup guides, support
4. **Expiration Watchdog:** automatic cleanup of expired subscriptions

## Status
On hold. Code preserved as-is for reference.

---
<details>
<summary>🇷🇺 По-русски</summary>

# 🌐 Maakolo Network

Защищённая VPN-инфраструктура с динамической маршрутизацией.
Командный проект (8 человек), временно **заморожен**.

## Технологии
- **Backend:** Python 3, Flask, PostgreSQL, Redis
- **Бот:** Python 3, aiogram 3 (асинхронный), aiosqlite
- **Ядро:** Xray-core / Sing-box
- **Протоколы:** VLESS (Reality), Hysteria 2 (QUIC)

## Безопасность (моя зона ответственности)
- **Аутентификация:** хэширование паролей с солью, опциональная 2FA, сессионные токены с временем жизни
- **Защита API:** rate limiting, валидация запросов, защита от злоупотреблений
- **Платежи:** безопасная проверка вебхуков, идемпотентная обработка, обнаружение мошенничества
- **Инфраструктура:** hardening сервера (SSH только по ключам, firewall, fail2ban)
- **Логирование:** маскирование секретов перед записью на диск

## Компоненты системы
1. **Flask API:** аккаунты, подписки, платежи, управление прокси
2. **PostgreSQL:** данные пользователей, подписки, реестр платежей
3. **Telegram-бот:** фронтенд (EN/RU/FI), гайды, поддержка
4. **Watchdog просрочек:** автоматическая очистка просроченных подписок

## Статус
На паузе. Код сохранён как есть для справки.

</details>
