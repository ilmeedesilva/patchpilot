# pydantic-demo

A tiny app written against **pydantic v1**, used to try out `patchpilot upgrade`.
Upgrading to pydantic v2 breaks it in three ways:

- `BaseSettings` moved to the separate `pydantic-settings` package
- `@root_validator` without `skip_on_failure=True` is an error in v2
- `.dict()` / `.parse_obj()` are deprecated in favour of `.model_dump()` / `.model_validate()`

See the main README for how to run patchpilot on it.
