# 🌍 Maakolo - Advanced Network Tunneling Research

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](https://opensource.org/licenses/MIT)
[![Tech Stack](https://img.shields.io/badge/Research-VLESS%20%7C%20Reality%20%7C%20Hysteria2-critical)](#)

### ⚠️ Academic & Educational Disclaimer

This project is developed strictly for educational and academic research purposes. Maakolo is an experimental tool designed to study modern network protocols, traffic encapsulation, and cryptographic mechanisms. It is intended for cybersecurity specialists, network engineers, and students to analyze network security structures in isolated, legally compliant environments.

The author(s) do not provide, promote, or endorse the use of this software for violating the terms of service of any network provider, bypassing regional network restrictions, or engaging in any illegal activities. Users are solely responsible for ensuring their use of this software complies with all applicable local, state, and federal laws.

### 📌 Project Overview

Maakolo is a cross-platform client-server architecture demonstrating the implementation of the VLESS protocol combined with the Reality security framework. The project explores how traffic can be securely encapsulated and mimics legitimate TLS connections to prevent DPI (Deep Packet Inspection) heuristic analysis.

#### Core Objectives:
* **Protocol Research:** Implementation and verification of high-performance VLESS/Hysteria2 proxy connections.
* **Traffic Masking:** Investigating the efficacy of TLS-mimicry structures against automated entropy-based traffic analysis.
* **Cross-Platform Implementation:** Deploying native network interfaces utilizing Flutter for mobile operating systems (Android/iOS).
* **Backend Resilience:** Load testing a Python-based API designed for stateless dynamic tunnel provisioning.

### 📅 Development Timeline
This project represents approximately **8 months** of iterative research, protocol testing, and infrastructure hardening. Development focused on practical implementation of traffic obfuscation techniques and resilient backend architecture under real-world network conditions.

### 📱 Client Application

The primary client is a **Flutter-based native application** engineered specifically for the Android ecosystem. It leverages platform-specific APIs for TUN-mode routing, persistent background execution, and OS-level network stack integration — features that are restricted or unavailable on iOS without enterprise provisioning.

**iOS & App-less Access:**  
For iOS users and those who prefer not to install native applications, the project includes a **full-featured Telegram bot** that serves as a lightweight, zero-install alternative. The bot provides complete account management, subscription provisioning, and secure key distribution through end-to-end encrypted Telegram channels. Initially developed as an interim solution during the early research phase, it has evolved into a permanent architectural component demonstrating server-side tunnel orchestration without client-side binaries.

The Flutter client source code is currently being prepared for public release and will be added to this repository in a forthcoming update. Pre-built binaries are distributed through official channels only.

### 🛠 Technical Stack
* **Client Frontend:** Flutter (Dart) 
* **Core Engine:** Xray-core / Sing-box framework
* **Backend API:** Python 3, Flask, PostgreSQL (Stateless Beta)
* **Network Interfaces:** TUN mode implementation for OS-level routing

### 🔐 Security & OpSec
This project does not collect, store, or process real user metadata. The backend architecture is specifically engineered to demonstrate stateless key generation, zero-logs compliance, and secure node handover.

