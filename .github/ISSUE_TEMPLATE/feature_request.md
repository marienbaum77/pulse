name:Feature request
description: Предложить новую возможность
title: "Feature: "
labels: ["enhancement"]
body:
  - type: markdown
    attributes:
      value: |
        Предложите новую идею. Мы приветствуем предложения, но учтите, что не все
        фичи попадают в roadmap.
  - type: textarea
    id: problem
    attributes:
      label: Проблема
      description: Какую проблему решает эта фича?
      placeholder: |
        Когда я ..., мне хочется, чтобы ...
    validations:
      required: true
  - type: textarea
    id: solution
    attributes:
      label: Решение
      description: Как вы это видите?
  - type: textarea
    id: alternatives
    attributes:
      label: Альтернативы
      description: Какие обходные пути есть сейчас?
  - type: textarea
    id: context
    attributes:
      label: Контекст
      description: Дополнительные детали, примеры, скриншоты.
