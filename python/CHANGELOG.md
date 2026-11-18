# Changelog

## [0.3.0](https://github.com/PabloAlaniz/omnilimit/compare/py-v0.2.0...py-v0.3.0) (2026-11-18)


### Features

* checkAll for several limits on one request, and createLockout ([#16](https://github.com/PabloAlaniz/omnilimit/issues/16)) ([2d306c5](https://github.com/PabloAlaniz/omnilimit/commit/2d306c59aa3d2809eebaf1afb25a7b5a0cc0bcbe))
* policy shorthand, FastAPI identity keys and an ASGI middleware ([#15](https://github.com/PabloAlaniz/omnilimit/issues/15)) ([c023b16](https://github.com/PabloAlaniz/omnilimit/commit/c023b161ece1d631a5f630f6019fdf363d4ca101))
* Postgres stores for SQLAlchemy and asyncpg, and a default timeout per store ([#18](https://github.com/PabloAlaniz/omnilimit/issues/18)) ([700f324](https://github.com/PabloAlaniz/omnilimit/commit/700f32406b14d86d73340569a703f84c5fde1b42))
* web adapter, per-request limiters, skip, clientKey and a store clock ([#14](https://github.com/PabloAlaniz/omnilimit/issues/14)) ([840aa0e](https://github.com/PabloAlaniz/omnilimit/commit/840aa0e03832440a9133497ce4663920ba6138e6))
* weighted cost for requests that count as several units ([#17](https://github.com/PabloAlaniz/omnilimit/issues/17)) ([be4866d](https://github.com/PabloAlaniz/omnilimit/commit/be4866dab446b24379ca066580ad384d474e85bf))

## [0.2.0](https://github.com/PabloAlaniz/omnilimit/compare/py-v0.1.1...py-v0.2.0) (2026-11-10)


### Features

* add peek, reset and onDenied ([#11](https://github.com/PabloAlaniz/omnilimit/issues/11)) ([36ab24d](https://github.com/PabloAlaniz/omnilimit/commit/36ab24d6608d5d0a23ccf046b957050256bc35c7))

## [0.1.1](https://github.com/PabloAlaniz/omnilimit/compare/py-v0.1.0...py-v0.1.1) (2026-11-06)


### Bug Fixes

* **python:** let stores pass as Store under strict mypy ([#7](https://github.com/PabloAlaniz/omnilimit/issues/7)) ([7ea460a](https://github.com/PabloAlaniz/omnilimit/commit/7ea460a7673ed7e4a368b44fceafaa8c3645ecc9))

## 0.1.0 (2026-11-06)


### Features

* **python:** add Python port ([#5](https://github.com/PabloAlaniz/omnilimit/issues/5)) ([f0256bc](https://github.com/PabloAlaniz/omnilimit/commit/f0256bc9084c95571a1757a1136ed9d0fe094555))
