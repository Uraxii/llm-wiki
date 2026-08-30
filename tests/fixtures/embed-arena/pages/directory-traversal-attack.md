---
kind: summary
title: Directory Traversal Attacks
identifiers:
  - Zip Slip
  - DotDotPwn
  - CWE Common Weakness Enumeration - Path Traversal
entities:
  - vulnerability: directory traversal
  - technique: percent encoding bypass
  - technique: double encoding attack
  - vulnerability: Zip Slip
  - platform: Windows
  - software: IIS
  - file: /etc/passwd
claims:
  - directory traversal | exploits | insufficient validation of user-supplied file names
  - PHP include() function | can be exploited by | ../ characters
  - Windows directory traversal | is limited to | a single partition
  - percent decoding before validation | can be bypassed by | patterns like %2e%2e/
  - double percent-encoding attack | replaces | ../ with %252E%252E%252F
  - overlong UTF-8 encodings | can lead to | directory traversal vulnerabilities in IIS
  - archive extraction code | should check for | path traversal in file paths
  - prevention algorithm | must compare | normalized full path to document root
kind_of_source: encyclopedia_entry
action: Developers must rigorously validate and normalize file paths derived from user input to prevent unauthorized file system access.
source: d1c6656b53a5
fetched: 2026-08-29
model: deepseek/deepseek-v3.2
prompt_fingerprint: 8e1e127378d2
---

A directory traversal attack exploits insufficient validation of user-supplied file names, allowing characters like `../` to access files outside an intended directory. Examples include reading system files like `/etc/passwd` via a vulnerable PHP `include()` function and the "Zip Slip" vulnerability affecting archive extraction. Attack variations involve bypassing filters through percent encoding, double encoding, or overlong UTF-8 sequences, particularly impacting software like Microsoft IIS. Prevention requires normalizing paths and strictly verifying they remain within a designated document root before allowing file access.
