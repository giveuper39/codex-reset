# Ввод токена с телефона

[← К README](../README.md)

Обычная установка запрашивает токен в терминале. Для владельца, у которого
под рукой только телефон, администратор хоста может запустить разовую HTTPS-форму
через `setup_web.py`.

## Условия

- Файлы проекта и Codex CLI уже размещены на хосте.
- Есть действующий сертификат для доменного имени или IP этого хоста.
- Временный TCP-порт разрешён в firewall и Security Group провайдера.
- Конфиг бота ещё не создан: форма не перезаписывает существующие токены.

## Запуск формы

Пример для сервисного пользователя `codex-reset`. Замените пути к сертификату
и ключу на действительные. Команды выполняются администратором на хосте:

```bash
cd /opt/codex-reset
SETUP_PATH="/setup/$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')"
echo "Откройте https://YOUR_HOST:9443${SETUP_PATH}"

sudo python3 setup_web.py \
  --config /var/lib/codex-reset/.config/codex-reset-bot/config.json \
  --cert /path/to/fullchain.pem \
  --key /path/to/private.key \
  --path "$SETUP_PATH" --port 9443 \
  --codex /var/lib/codex-reset/.local/bin/codex \
  --state-dir /var/lib/codex-reset/.local/state/codex-reset-bot \
  --reset-dir /var/lib/codex-reset/.local/state/codex-reset-bot/redemptions \
  --uid "$(id -u codex-reset)" --gid "$(id -g codex-reset)"
```

Ключ сертификата загружается до отказа от привилегий. Конфиг сохраняется
от имени сервисного пользователя с правами `600`.

Откройте полученную ссылку с телефона, вставьте токен из BotFather и сохраните.
Форма проверит токен через `getMe`, покажет ссылку для привязки владельца
и выключится. Без успешного ввода она работает не дольше 20 минут.
Токен не возвращается в HTML и не попадает в access log.

После сохранения администратор запускает установленный сервис:

```bash
sudo systemctl enable --now codex-reset-bot
```

Откройте ссылку владельца в Telegram и нажмите **Start**. Временный порт
и правило SG после этого можно закрыть: обычному боту нужны только исходящие
соединения.
