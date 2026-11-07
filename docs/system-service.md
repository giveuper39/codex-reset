# Системный сервис на VPS/VDS

[← К README](../README.md)

Этот вариант подходит для сервера с root-доступом. Скрипт работает под отдельным
пользователем `codex-reset`, а таймеры управляются системным systemd.
Файлы программы принадлежат root; вход в Codex и состояние находятся
в `/var/lib/codex-reset`.

## 1. Подготовьте файлы

На Ubuntu/Debian, из обычной SSH-сессии с `sudo` или под root:

```bash
sudo apt-get update
sudo apt-get install -y python3 git curl bubblewrap
git clone https://github.com/giveuper39/codex-reset-watch.git
cd codex-reset-watch

id codex-reset >/dev/null 2>&1 || sudo useradd --system --create-home \
  --home-dir /var/lib/codex-reset --shell /usr/sbin/nologin codex-reset

sudo install -d -o root -g root -m 755 /opt/codex-reset
sudo install -m 644 codex_reset.py /opt/codex-reset/
sudo install -m 644 systemd/system/codex-reset@.service /etc/systemd/system/
sudo install -m 644 systemd/codex-reset@.timer /etc/systemd/system/
sudo systemctl daemon-reload
```

Если пользователь `codex-reset` уже существует, убедитесь, что его домашняя
папка — `/var/lib/codex-reset` и он выделен для этой задачи.

## 2. Установите CLI и войдите

Перейдите в доступный сервисному пользователю каталог перед запуском команд:

```bash
cd /opt/codex-reset

sudo -u codex-reset -H env CODEX_NON_INTERACTIVE=1 \
  bash -o pipefail -c 'curl -fsSL https://chatgpt.com/codex/install.sh | sh'

sudo -u codex-reset -H /var/lib/codex-reset/.local/bin/codex login --device-auth
sudo -u codex-reset -H /var/lib/codex-reset/.local/bin/codex login status
```

Завершите вход на официальной странице по ссылке из терминала. Доступ к аккаунту
хранится в домашней папке сервисного пользователя. Относитесь к кэшу входа
как к паролю: сохраняйте его вне репозитория и переписок.

## 3. Проверьте список и включите таймер

```bash
sudo -u codex-reset -H python3 /opt/codex-reset/codex_reset.py \
  --codex /var/lib/codex-reset/.local/bin/codex

CREDIT_ID='RateLimitResetCredit_example'
sudo systemctl enable --now "codex-reset@${CREDIT_ID}.timer"
```

Замените пример на **точный ID из вывода**. Включение таймера разрешает
применение только этого ресета при условиях, описанных в README.
Для нескольких ID включите отдельный таймер на каждый.

Системные таймеры работают после выхода из SSH и запускаются при загрузке;
`loginctl enable-linger` для этого варианта не требуется.

## Управление

```bash
sudo systemctl list-timers 'codex-reset@*'
sudo journalctl -u "codex-reset@${CREDIT_ID}.service" -n 30 --no-pager
sudo systemctl disable --now "codex-reset@${CREDIT_ID}.timer"
```

Состояние: `/var/lib/codex-reset/.local/state/codex-reset/`.
Сервис использует `NoNewPrivileges`, приватный `/tmp` и запись только
в домашнюю папку сервисного пользователя.
