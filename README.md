# 🌐 Maakolo Network

Secure VPN infrastructure with dynamic user routing.
Team pet project (8 people, ~8 months), currently **on hold**.

## Tech Stack
- **Backend:** Python 3, Flask, PostgreSQL, Redis
- **Bot:** Python 3, aiogram 3 (async), aiosqlite
- **Core:** Xray-core / Sing-box
- **Protocols:** VLESS (Reality), Hysteria 2 (QUIC)

## My Responsibility
- Permanently responsible for infrastructure throughout the whole project (~8 months, team of 8)
- Deployed and maintained the VPN server (VPS); planned a two-server architecture: backend/auth separate from VPN tunnels
- Built the REST API on Flask, worked with PostgreSQL, integrated webhooks for subscriptions
- Implemented authentication with secure password hashing together with a partner
- Hardened the server: disabled root SSH login, key-based auth only, non-default SSH port, fail2ban, firewall with a minimal set of open ports
- Diagnosed and fixed production outages: troubleshooting and infrastructure recovery after incidents

## System Components
1. **Flask API:** accounts, subscriptions, proxy management
2. **PostgreSQL:** user data, subscriptions
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

## Моя зона ответственности
- Постоянный ответственный за инфраструктуру на всём протяжении проекта (~8 месяцев, команда из 8 человек)
- Развернул и поддерживал VPN-сервер (VPS); в архитектуре планировалось разделение на 2 сервера: отдельно backend/авторизация, отдельно VPN-туннели
- Настраивал REST API на Flask, работал с PostgreSQL, интегрировал вебхуки для подписок
- Совместно с напарником реализовал систему аутентификации с безопасным хэшированием паролей
- Провёл hardening сервера: отключил root-доступ по SSH, настроил вход по SSH-ключам, сменил стандартный порт SSH, установил fail2ban, настроил firewall под минимально необходимый набор портов
- Диагностировал и устранял сбои в проде: troubleshooting и восстановление инфраструктуры после инцидентов

## Компоненты системы
1. **Flask API:** аккаунты, подписки, управление прокси
2. **PostgreSQL:** данные пользователей, подписки
3. **Android APK:** нативный клиент для Android (вход по ID + паролю, встроенный клиент протокола)
4. **Telegram-бот:** клиент для iOS/PC и fallback (EN/RU/FI), гайды, тикеты в поддержку, выдача APK
5. **Watchdog просрочек:** автоматическая очистка просроченных подписок

## Статус
На паузе. Код сохранён как есть для справки.

</details>
