---
kind: summary
title: Sandbox (Computer Security)
identifiers: [seccomp, cgroups, namespaces, User Account Control, SELinux, Apparmor, Google Native Client, Secure Computing Mode, HTML5, .NET Common Language Runtime, Software Fault Isolation]
entities:
  - concept: sandbox (computer security)
  - technique: virtualization
  - technique: containerization
  - technique: capability-based security
  - platform: Linux
  - platform: Android
  - platform: iOS/iPadOS
  - platform: Windows
  - platform: macOS
  - vulnerability: system failure
  - vulnerability: software vulnerability
claims:
  - sandbox | is a | security mechanism for separating running programs
  - sandbox | is derived from metaphor of | a child's sandbox
  - sandbox | is used to | analyze untrusted or untested code
  - sandbox | provides | tightly controlled set of resources
  - sandboxing | can be comparable to | virtualization
  - sandboxing | is frequently used to | test for malware
  - sandbox | is implemented by | executing software in restricted OS environment
  - examples of implementations | include | Linux seccomp/cgroups/namespaces, Android app sandbox, Apple App Sandbox, Windows UAC, virtual machines
  - security researchers | rely on | sandboxing to analyze malware behavior
kind_of_source: article
action: Understand that a sandbox is a fundamental security isolation mechanism implemented across major platforms to safely run untrusted code.
source: 3e4e15cbfcb3
fetched: 2026-08-29
model: deepseek/deepseek-v3.2
prompt_fingerprint: 8e1e127378d2
---

A sandbox is a computer security mechanism that isolates running programs within a tightly controlled environment to prevent system failures or vulnerabilities from spreading. It is commonly used to safely execute and analyze untested or potentially malicious code without endangering the host system. Implementations are diverse, built into operating systems like Android, iOS, and Windows, and leveraging kernel features like seccomp and cgroups on Linux. Key use cases range from application containment and malware research to secure code execution in web browsers and online judging systems.
