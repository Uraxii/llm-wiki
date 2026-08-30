---
kind: summary
title: Cryptographic Salts
identifiers: []
entities:
- concept: cryptographic salt
- concept: one-way function
- attack: precomputed table attack
- concept: rainbow table
- cryptographic_primitives: cryptographic hash function
- cryptographic_primitives: cryptographically secure pseudorandom number generator (CSPRNG)
claims:
- Salting | defends against | attacks that use precomputed tables (e.g., rainbow tables)
- Salting | protects against | identical password hashes in a database
- A salt | is | random data
- A salt | is fed as additional input to | a one-way function that hashes a password
- Salting | does not place any burden on | users
- A unique salt | is typically | randomly generated for each password
- The salt and password | are concatenated and fed to | a cryptographic hash function
- The salt | does not need to be | encrypted
- Using the same salt for all passwords | is | dangerous
- A short salt | may allow an attacker to | precompute a table for every possible salt
kind_of_source: definition
action: Implement unique, randomly generated salts of sufficient length (e.g., 16+ bytes) for each hashed password to protect against precomputed table attacks and prevent hash correlation.
source: aa42b6882755
fetched: 2026-08-29
model: deepseek/deepseek-v3.2
prompt_fingerprint: 8e1e127378d2
---

A cryptographic salt is random data used as an additional input to a one-way function that hashes a password or passphrase. Its primary purpose is to defend against attacks using precomputed tables, like rainbow tables, by making such tables impractically large to create, and to ensure identical passwords do not produce identical hash values within a database, thus protecting users who share common passwords. A unique salt is generated for each password, combined with the password, and then hashed, with the resulting hash and the unencrypted salt stored together. Common mistakes to avoid include reusing the same salt across multiple passwords and using salts that are too short, with a recommended minimum length of 16 bytes for security.
