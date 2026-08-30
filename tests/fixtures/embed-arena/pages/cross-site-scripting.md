---
kind: summary
title: Cross-Site Scripting (XSS) Vulnerability
identifiers: [OWASP]
entities:
  - vulnerability: cross-site scripting
  - attack: XSS attack
  - policy: same-origin policy
claims:
  - cross-site scripting | is a type of | security vulnerability
  - cross-site scripting | can be found in | some web applications
  - XSS attacks | enable attackers to | inject client-side scripts into web pages viewed by other users
  - a cross-site scripting vulnerability | may be used to | bypass access controls such as the same-origin policy
  - XSS effects | vary in range from | petty nuisance to significant security risk
kind_of_source: definition
action: Web developers and security professionals should understand XSS to implement proper input validation and output encoding to prevent attackers from injecting malicious scripts.
source: a3ad4516d5ab
fetched: 2026-08-29
model: deepseek/deepseek-v3.2
prompt_fingerprint: b44ddc9d9f4f
---

Cross-site scripting (XSS) is a security vulnerability in web applications that allows attackers to inject malicious client-side scripts into pages viewed by other users, potentially bypassing security controls like the same-origin policy. The impact of such an attack can range from a minor nuisance to a major security risk, depending on the sensitivity of the site's data and the security measures in place.
