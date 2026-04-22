# Schema — Aurora Updates

`latest.schema.json` — JSON Schema draft-07 для `<product>/latest.json`.

## Валидация локально

```bash
pip install check-jsonschema
check-jsonschema --schemafile schema/latest.schema.json aurora-econometrica-gui/latest.json
```

## CI

`.github/workflows/validate.yml` прогоняет все `*/latest.json` на каждом push/PR в `main`.

## Evolution

При добавлении нового **required** поля — старые `latest.json` упадут в CI.
Migration: либо сделать поле `optional`, либо сразу обновить все 13 продуктов в одном PR.

## Fields

| Поле | Тип | Обязательно | Описание |
|------|-----|-------------|----------|
| `version` | string (semver) | ✅ | `1.2.3` или `1.2.3-rc1` |
| `download_url` | HTTPS URL | ✅ | Ссылка на `.exe` installer |
| `checksum` | `sha256:<64hex>` | ✅ | SHA256 installer'а |
| `release_notes` | string ≤2000 | — | Для клиентов |
| `mandatory` | boolean | ✅ | Принудительное обновление |
| `min_version` | string (semver) | ✅ | Минимальная версия клиента |
| `redirect_product` | string (slug) | — | Переадресация на другой продукт (для переименований) |
