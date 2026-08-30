---
kind: summary
title: bcrypt Password-Hashing Function
identifiers:
  - bcrypt
  - Niels Provos
  - David Mazières
  - Blowfish
  - USENIX
  - OpenBSD
  - SUSE Linux
entities:
  - algorithm: bcrypt
  - person: Niels Provos
  - person: David Mazières
  - cipher: Blowfish
  - conference: USENIX
  - operating_system: OpenBSD
  - operating_system: SUSE Linux
claims:
  - bcrypt | designed by | Niels Provos and David Mazières
  - bcrypt | based on | the Blowfish cipher
  - bcrypt | presented at | USENIX in 1999
  - bcrypt | incorporates | a salt
  - bcrypt | protects against | rainbow table attacks
  - bcrypt | is | an adaptive function
  - bcrypt | can have increased | iteration count over time
  - bcrypt | remains resistant to | brute-force search attacks
  - bcrypt | is the default password hash algorithm for | OpenBSD
  - bcrypt | was the default for | some Linux distributions such as SUSE Linux
  - bcrypt | has implementations in | C, C++
kind_of_source: description
action: Use bcrypt for secure password storage due to its built-in salt and adaptive, computation-intensive design that resists brute-force and rainbow table attacks.
source: 46f5fd275d93
fetched: 2026-08-29
model: deepseek/deepseek-v3.2
prompt_fingerprint: b44ddc9d9f4f
---

bcrypt is a password-hashing function based on the Blowfish cipher, designed with a salt and adaptive work factor to resist rainbow table and brute-force attacks. It is the default password hash for OpenBSD and has been for some Linux distributions, with implementations available in languages like C and C++.
