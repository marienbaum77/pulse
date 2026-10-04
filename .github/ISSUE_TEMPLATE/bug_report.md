name:Bug report
description: Сообщить о воспроизводимой проблеме
title: "Bug: "
labels: ["bug"]
body:
  - type: markdown
    attributes:
      value: |
        Спасибо за обращение! Перед тем как создать issue, убедитесь, что проблема
        воспроизводится на последней версии и ещё не описана в [открытых issue](https://github.com/marienbaum77/pulse/issues).
  - type: textarea
    id: description
    attributes:
      label: Описание
      description: Что произошло и что ожидалось?
      placeholder: |
        **Что произошло**
        ...
        **Что ожидалось**
        ...
    validations:
      required: true
  - type: textarea
    id: steps
    attributes:
      label: Шаги воспроизведения
      description: Как воспроизвести проблему?
      value: |
        1.
        2.
        3.
    validations:
      required: true
  - type: textarea
    id: logs
    attributes:
      label: Логи и скриншоты
      description: Скопируйте вывод `docker compose logs` или приложите скриншот.
      render: bash
  - type: dropdown
    id: platform
    attributes:
      label: Платформа
      options:
        - Docker Compose (ручной запуск)
        - Десктоп (Windows)
        - VPS (Ubuntu + Caddy)
    validations:
      required: true
  - type: input
    id: version
    attributes:
      label: Версия
      description: Тег релиза или SHA коммита
      placeholder: v1.0.0
