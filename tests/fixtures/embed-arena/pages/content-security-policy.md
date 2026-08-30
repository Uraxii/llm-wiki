---
kind: summary
title: Content Security Policy (CSP) Overview
identifiers: [Content Security Policy, CSP, W3C, X-WebKit-CSP, X-Content-Security-Policy]
entities: [standard: Content Security Policy, vulnerability: cross-site scripting, attack: code injection]
claims: [Content Security Policy | is a | security standard, Content Security Policy | prevents | cross-site scripting, Content Security Policy | is supported by | modern web browsers, Content Security Policy | declares | approved origins of content]
kind_of_source: wiki
action: Understand how CSP works to prevent code injection attacks, what its deployment involves, and its historical development and browser support.

source: dae0f6b7e77f
fetched: 2026-08-29
model: deepseek/deepseek-v3.2
prompt_fingerprint: 8e1e127378d2
---

Content Security Policy (CSP) is a W3C security standard designed to prevent cross-site scripting and other code injection attacks by allowing websites to specify trusted origins for content like scripts and images. Introduced in 2004 and implemented in browsers like Firefox and Chrome, CSP has evolved through multiple levels, with Level 3 under development as of 2023. It operates via HTTP headers or meta tags, disabling features like inline JavaScript and `eval()` by default, which may require refactoring for existing applications. The policy includes a reporting mechanism for violations and historically included exemptions for browser extensions, though this recommendation has been softened in later versions.
