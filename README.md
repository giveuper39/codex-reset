# Codex Reset Watch

**Автоматическое применение banked reset перед сгоранием.**

[![Checks](https://github.com/giveuper39/codex-reset-watch/actions/workflows/checks.yml/badge.svg)](https://github.com/giveuper39/codex-reset-watch/actions/workflows/checks.yml)
![Python](https://img.shields.io/badge/Python-3.10%2B-3776AB?logo=python&logoColor=white)
![Platform](https://img.shields.io/badge/Linux-systemd-555555?logo=linux&logoColor=white)
[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)

Небольшой Python-скрипт для VPS/VDS. Проверяет срок действия накопленных
ресетов Codex и применяет выбранный ресет незадолго до истечения срока.
Работает через Codex App Server, без запросов к модели и сторонних Python-библиотек.

[Установка](#установка) · [Управление](#управление) · [Как выбирается ресет](#как-выбирается-ресет) · [Системный сервис](docs/system-service.md)

> [!IMPORTANT]
> Применение разрешается для **конкретного `creditId`**. Он должен быть ближайшим
> к сгоранию среди всех доступных ресетов. Случайный выбор на стороне сервера
> не используется.

## Как это работает

| Настройка | По умолчанию |
| :--- | :--- |
| Проверка | Примерно раз в минуту |
| Окно применения | Последние 15 минут до `expiresAt` |
| Остаток основного лимита | Не больше 10% в недельном или пятичасовом окне |
| Разрешение | Отдельно для каждого ID |
| Запуск | systemd; сохраняется после выхода из SSH и перезагрузки |

Если условия не выполнены, скрипт ждёт следующей проверки. Обычное обновление
лимита происходит на стороне Codex; эта утилита управляет выданными **banked reset**.

## Установка

Нужны Linux с systemd, Python **3.10+** и аккаунт ChatGPT с доступом к Codex.
Основной вариант ниже выполняется под обычным пользователем с `sudo`.
Для установки через root с отдельным сервисным пользователем есть [отдельный гайд](docs/system-service.md).

### 1. Скачайте проект

Для Ubuntu/Debian:

```bash
sudo apt-get update
sudo apt-get install -y python3 git curl bubblewrap

git clone https://github.com/giveuper39/codex-reset-watch.git
cd codex-reset-watch
```

### 2. Установите Codex CLI и войдите в аккаунт

Если CLI уже установлен, достаточно проверить вход. Для новой установки:

```bash
curl -fsSL https://chatgpt.com/codex/install.sh | sh
export PATH="$HOME/.local/bin:$PATH"

codex login --device-auth
codex login status
```

Откройте ссылку из терминала и введите одноразовый код. Device code login
должен быть разрешён в настройках безопасности ChatGPT или вашего workspace.
Используется вход **через ChatGPT**, а не API-ключ.

### 3. Установите скрипт и таймер

Из папки проекта:

```bash
mkdir -p ~/.local/bin ~/.config/systemd/user
install -m 700 codex_reset.py ~/.local/bin/codex_reset.py
install -m 644 systemd/codex-reset@.service systemd/codex-reset@.timer \
  ~/.config/systemd/user/

systemctl --user daemon-reload
sudo loginctl enable-linger "$USER"
```

Linger позволяет сервису работать после закрытия SSH и запускаться при загрузке.
Если `command -v codex` показывает нестандартный каталог, например nvm,
добавьте его в `Environment=PATH` установленного `.service` и выполните `daemon-reload`.

### 4. Посмотрите доступные ресеты

```bash
python3 ~/.local/bin/codex_reset.py
```

Это режим **только чтения**. Пример вывода:

```json
{"creditId": "RateLimitResetCredit_example", "expiresAtUTC": "2027-01-30T20:00:00+00:00", "earliest": true}
```

`earliest: true` отмечает ближайший срок сгорания. Все даты в выводе — **UTC**.

### 5. Разрешите применение выбранного ресета

Подставьте настоящий ID из предыдущего шага:

```bash
CREDIT_ID='RateLimitResetCredit_example'
systemctl --user enable --now "codex-reset@${CREDIT_ID}.timer"
```

Эта команда разрешает однократное применение **именно этого ресета** при выполнении
условий. Для второго ID повторите команду. Более поздний ресет будет ждать,
пока он не станет ближайшим и не войдёт в своё окно применения.

## Управление

Используйте ту же переменную `CREDIT_ID`, что и при включении таймера.

```bash
# Расписание
systemctl --user list-timers 'codex-reset@*'

# Последние сообщения выбранного сервиса
journalctl --user -u "codex-reset@${CREDIT_ID}.service" -n 30 --no-pager

# Отключить автоприменение этого ресета
systemctl --user disable --now "codex-reset@${CREDIT_ID}.timer"
```

После сообщения `Applied and verified exact credit` таймер можно отключить.
Новые выданные ресеты подключаются отдельно по их ID.

<details>
<summary><strong>Параметры ручного запуска</strong></summary>

```bash
python3 codex_reset.py --help
python3 codex_reset.py --codex /absolute/path/to/codex
```

| Параметр | Назначение |
| :--- | :--- |
| `--apply` | Разрешить применение; требует `--credit-id` |
| `--credit-id ID` | Точный ID разрешённого ресета |
| `--before-minutes N` | Размер окна до сгорания, от 1 до 1440 минут; по умолчанию 15 |
| `--codex PATH` | Путь к исполняемому файлу Codex |
| `--state-dir PATH` | Каталог состояния; по умолчанию `~/.local/state/codex-reset` |

Изменение `--before-minutes` в таймере делается через `ExecStart` его сервиса.
Сам по себе флаг `--credit-id` без `--apply` ничего не списывает.

</details>

## Как выбирается ресет

1. Получается полный список доступных ID и сроков действия. Неполный список,
   неизвестный тип, неизвестный срок или повторяющийся ID останавливают проверку.
2. Выбирается минимальный будущий `expiresAt`. Просроченные и явно бессрочные
   ресеты пропускаются. При равных датах выбор определяется ID.
3. Выбранный ID сравнивается с разрешённым `--credit-id`.
4. Проверяются окно до сгорания и остаток текущего основного лимита Codex.
5. В схеме установленного CLI проверяется поддержка **`creditId`**.
   Запрос применения всегда содержит этот ID.
6. До запроса сохраняются ID и `idempotencyKey`. Неопределённый сетевой ответ
   повторяется с тем же ключом, без перехода на другой ресет.
7. После успеха перечитывается список: выбранный ID должен исчезнуть,
   остальные — сохраниться. Подтверждённое списание больше не повторяется.

Срок **сгорания ресета** берётся из `expiresAt`. Поле `resetsAt` относится
к обычному обновлению лимита и используется для проверки актуальности окна.

## Ограничения

- Если остаток лимита не опустится до ≤10%, ресет может сгореть неприменённым.
- Серверу нужны сеть, верное время и действующий вход в Codex. Работа в последнюю
  секунду не гарантируется; 15 минут оставляют время для повторов.
- Состояние в `~/.local/state/codex-reset` содержит ключи повторных запросов.
  Сохраняйте его, особенно после неопределённого ответа сервера.
- Если после неопределённого ответа ID исчез, требуется ручная проверка.
  Постпроверка обнаруживает несовпадения, но не отменяет выполненный reset.
- Тесты используют имитацию сервера. Реальные чтение и запуск через systemd
  проверены на Ubuntu 24.04; реальное списание в рамках проверки не выполнялось.

## Разработка

```bash
python3 -m unittest -v
```

13 тестов проверяют выбор ближайшего срока, точный ID, неполные данные,
условия применения, сетевые повторы и проверку результата. Токены и аккаунт
для тестов не нужны. GitHub Actions запускает их на Python 3.10 и 3.13.

## Документация

- [Codex App Server: `creditId`, `idempotencyKey` и работа с ресетами](https://learn.chatgpt.com/docs/app-server)
- [Вход на сервере без браузера](https://learn.chatgpt.com/docs/auth?surface=cli)
- [Codex CLI](https://learn.chatgpt.com/docs/codex/cli)

Независимая утилита; не является продуктом OpenAI. Лицензия [MIT](LICENSE).
