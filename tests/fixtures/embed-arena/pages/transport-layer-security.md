---
kind: summary
title: Transport Layer Security (TLS) Protocol Overview
identifiers:
  - TLS
  - DTLS
  - SSL
  - TLS 1.3
  - IETF
  - HTTPS
  - STARTTLS
  - Netscape Navigator
entities:
  - protocol: Transport Layer Security (TLS)
  - protocol: Datagram Transport Layer Security (DTLS)
  - protocol: Secure Sockets Layer (SSL)
  - organization: Internet Engineering Task Force (IETF)
  - standard: TLS 1.3
  - technique: Diffie-Hellman key exchange
  - property: forward secrecy
  - port: 443
kind_of_source: article
action: Understand the purpose, operation, and core security properties of the TLS protocol for securing network communications.
source: 419b7701b179
fetched: 2026-08-29
model: deepseek/deepseek-v3.2
prompt_fingerprint: 8e1e127378d2
---

Transport Layer Security (TLS) is a foundational cryptographic protocol designed to secure communications over networks like the Internet, most visibly used for HTTPS. It provides privacy, integrity, and authenticity between communicating applications, operating between the transport and presentation layers and consisting of TLS handshake and record protocols. A TLS connection is established through a handshake where the client and server negotiate cipher suites, authenticate the server via certificate, and establish a unique session key, with methods like Diffie-Hellman enabling forward secrecy. TLS builds upon the deprecated SSL specifications and is standardized by the IETF, with TLS 1.3 being the current version as of 2018.
