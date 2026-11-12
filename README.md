# Antigravity Session Manager

Утилита с графическим интерфейсом (GUI) и командной строкой (CLI) для **выгрузки (экспорта)** и **заливки (импорта)** сессий Google Antigravity.

## Что входит в слепок сессии
Утилита собирает и восстанавливает все компоненты сессии из `~/.gemini/antigravity`:
1. **`conversations/<id>.db`** — SQLite-база шагов траектории (экспортируется через безопасный `sqlite3.backup()` без блокировок даже на активной сессии).
2. **Дочерние сессии сабагентов** — автоматически находит и привязывает все дочерние траектории (`parent_conversation_id`), вызванные внутри сессии (`browser`, `image-generator`, `research` и др.).
3. **`brain/<id>/`** — все артефакты, сгенерированные файлы и полные логи (`transcript.jsonl`, `transcript_full.jsonl`).
4. **`annotations/<id>.pbtxt` и `chapter_titles/<id>.pb`** — заголовки и метки времени просмотра.
5. **`conversation_summaries.db` + `agyhub_summaries_proto.pb`** — при заливке автоматически обновляет SQLite-таблицу саммари и пересобирает бинарный protobuf-индекс `agyhub_summaries_proto.pb`, чтобы импортированная сессия сразу отобразилась в боковой панели Antigravity.

## Запуск GUI
Дважды кликните по файлу **`run_gui.bat`** или выполните:
```powershell
python C:\Users\clash\.gemini\antigravity\scratch\agy-session-manager\agy_session_manager.py
```

## Использование из командной строки (CLI)
```powershell
# Список всех сессий
python agy_session_manager.py list

# Выгрузить конкретную сессию (можно указать первые символы ID)
python agy_session_manager.py export e3990465 -o my_session.agysession.zip

# Выгрузить все сессии разом
python agy_session_manager.py export --all -o all_sessions.agysession.zip

# Проверить содержимое архива
python agy_session_manager.py inspect my_session.agysession.zip

# Залить сессии из архива
python agy_session_manager.py import my_session.agysession.zip
```
