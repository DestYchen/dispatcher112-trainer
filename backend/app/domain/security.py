from argon2 import PasswordHasher

# OWASP Argon2id minimum: 19 MiB, two iterations, one lane.
password_hasher = PasswordHasher(time_cost=2, memory_cost=19456, parallelism=1)
