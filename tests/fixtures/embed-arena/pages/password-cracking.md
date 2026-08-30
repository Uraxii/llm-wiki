---
kind: summary
title: Password Cracking Overview and Methods
identifiers: [NTLM, John the Ripper, distributed.net, RC5, bcrypt, scrypt, Argon2, MD5, SHA]
entities:
  - technique: password cracking
  - technique: brute-force attack
  - technique: password spraying
  - technique: dictionary attack
  - technique: offline attack
  - vulnerability: weak password entropy
  - countermeasure: bcrypt
  - countermeasure: account lockout
  - concept: password hash
claims:
  - password cracking | is the process of | guessing passwords protecting a computer system
  - brute-force attack | is a common approach where one | repeatedly tries guesses and checks them against a cryptographic hash
  - password spraying | evades account lockout by | guessing the same password across many accounts with delays between attempts
  - offline attacks | are possible and more effective when | an attacker obtains a password hash file
  - time to crack a password | is related to | bit strength (password entropy) and the hashing function used
  - GPU-based tools | can speed up password cracking | by a factor of 50 to 100 over general-purpose computers for specific algorithms
  - suitable password hashing functions (e.g., bcrypt) | are many orders of magnitude better than | naive functions like MD5 or SHA
kind_of_source: encyclopedia
action: System administrators and users should understand that the time to crack a password depends on its entropy and the strength of the hashing function, and should therefore use strong, unique passwords combined with modern, slow hashing algorithms like bcrypt or Argon2.
source: fadf7d2a8a72
fetched: 2026-08-29
model: deepseek/deepseek-v3.2
prompt_fingerprint: 8e1e127378d2
---

Password cracking is the process of recovering passwords from computer systems, primarily through methods like brute-force attacks, which try every possible combination, or more optimized approaches like dictionary attacks and password spraying. The effectiveness of cracking depends heavily on whether the attacker can obtain a cryptographic hash for an offline attack, bypassing account lockout defenses. The time required is a function of the password's bit strength and the specific hashing algorithm used; weak functions like MD5 are vulnerable to rapid cracking, especially with GPU-accelerated tools. Modern, deliberately slow hashing functions like bcrypt significantly increase the computational cost of cracking, serving as a critical defense. The work also notes that distributed computing networks and botnets can vastly extend cracking capabilities, emphasizing the need for robust password policies.
