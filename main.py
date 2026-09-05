"""Точка входа для панелей и PaaS, запускающих файл, а не модуль.

Панели вроде Pterodactyl вызывают `python main.py`, а не `python -m app.main`,
и путь к файлу задать нельзя — только имя. При запуске файлом (`python
app/main.py`) корень проекта не попадает в `sys.path`, и `from app.config
import ...` падает с ModuleNotFoundError. Этот файл лежит в корне, поэтому
корень уже в `sys.path` штатно, и достаточно позвать `main()` из app.main.
"""

from app.main import main

if __name__ == "__main__":
    main()
