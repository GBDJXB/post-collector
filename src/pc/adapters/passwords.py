"""密码哈希适配器；只使用 Argon2id，验证失败返回确定结果。"""

from uuid import uuid4

from argon2 import PasswordHasher, Type
from argon2.exceptions import InvalidHashError, VerificationError

_HASHER = PasswordHasher(time_cost=2, memory_cost=19456, parallelism=1, type=Type.ID)
_DUMMY_HASH = _HASHER.hash(str(uuid4()))


def hash_password(password: str) -> str:
    return _HASHER.hash(password)


def verify_password(hashed: str | None, password: str) -> bool:
    # 不存在或无资格的账号也执行相同哈希算法，减少账号枚举时序差异。
    try:
        return _HASHER.verify(hashed or _DUMMY_HASH, password)
    except (VerificationError, InvalidHashError):
        return False
