"""At-rest credential encryption."""

from robber.security.perimeter import SelfModificationProtectedError, self_edit_allowed, validate_file_mutation
from robber.security.vault import CredentialVault, bind_vault, get_vault, iter_secured_keys, unlock_master

__all__ = [
    "CredentialVault",
    "SelfModificationProtectedError",
    "bind_vault",
    "get_vault",
    "iter_secured_keys",
    "self_edit_allowed",
    "unlock_master",
    "validate_file_mutation",
]
