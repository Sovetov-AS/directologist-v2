"""Секреты только в явно выбранном native OS backend; CLI чтения секрета отсутствует."""
import platform
from dataclasses import dataclass, field

from .contracts import ContractError, PROVIDERS, identifier


class SecretError(ContractError):
    def __init__(self):
        super().__init__("Защищённое хранилище ОС недоступно. Проверьте Keychain / Credential Locker / Secret Service; текстовый fallback запрещён.")


@dataclass(repr=False)
class Credential:
    value: str = field(repr=False)

    def __post_init__(self):
        if not isinstance(self.value, str) or not self.value.strip() or len(self.value) > 8192:
            raise ContractError("Пустой или слишком длинный ключ.")
        if any(ord(c) < 33 or ord(c) > 126 for c in self.value):
            raise ContractError("Ключ содержит пробелы или недопустимые символы.")

    def __repr__(self):
        return "<Credential hidden>"


class SecretStore:
    def __init__(self, project_id: str):
        self.project_id = identifier(project_id)
        try:
            system=platform.system()
            if system == "Darwin":
                from keyring.backends.macOS import Keyring
            elif system == "Windows":
                from keyring.backends.Windows import WinVaultKeyring as Keyring
            elif system == "Linux":
                from keyring.backends.SecretService import Keyring
            else:
                raise SecretError()
            # Не выбирать автоматически backend из config/env/entrypoints.
            self._backend = Keyring()
            if system == "Darwin": self._backend.keychain = None
            if self._backend.priority <= 0:
                raise SecretError()
        except Exception:
            raise SecretError() from None

    def _key(self, provider: str, connection_id: str) -> tuple[str, str]:
        if provider not in PROVIDERS:
            raise ContractError("Неизвестная площадка.")
        identifier(connection_id)
        return f"pro.1pi.directologist.v2/{self.project_id}/{provider}", connection_id

    def put(self, provider: str, connection_id: str, credential: Credential) -> None:
        service, account = self._key(provider, connection_id)
        try:
            self._backend.set_password(service, account, credential.value)
            if self._backend.get_password(service, account) != credential.value:
                raise SecretError()
        except Exception:
            raise SecretError() from None

    def get(self, provider: str, connection_id: str) -> Credential | None:
        service, account = self._key(provider, connection_id)
        try:
            value = self._backend.get_password(service, account)
            return Credential(value) if value is not None else None
        except Exception:
            raise SecretError() from None

    def delete(self, provider: str, connection_id: str) -> None:
        service, account = self._key(provider, connection_id)
        try:
            if self._backend.get_password(service, account) is not None:
                self._backend.delete_password(service, account)
            if self._backend.get_password(service, account) is not None:
                raise SecretError()
        except Exception:
            raise SecretError() from None
