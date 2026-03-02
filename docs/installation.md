<!-- Auto-generated from https://mkbrr.com/installation — run scripts/update-mkbrr-docs.sh to refresh -->

# Installation

> Source: <https://mkbrr.com/installation>

How to install mkbrr using various methods.

---

## Choose Your Installation Method

Select the installation method that best suits your system and preferences:

- **Pre-built Binaries** — Download and install pre-compiled CLI binaries
- **Package Managers** — Install using your preferred package manager
- **Docker** — Install using Docker *(used by mkbrr-wizard)*
- **GUI Application** — Desktop application for visual torrent management

---

## Pre-built Binaries

Download the appropriate binary for your system from the
[GitHub Releases page](https://github.com/autobrr/mkbrr/releases/latest) or use the commands below.

### Linux

> Make sure the extraction directory (e.g., `/usr/local/bin`) is included in your system's `PATH`.

**x86_64**

```bash
wget $(curl -s https://api.github.com/repos/autobrr/mkbrr/releases/latest | grep download | grep linux_x86_64 | cut -d\" -f4)
sudo tar -C /usr/local/bin -xzf mkbrr_*_linux_x86_64.tar.gz mkbrr
```

**arm64**

```bash
wget $(curl -s https://api.github.com/repos/autobrr/mkbrr/releases/latest | grep download | grep linux_arm64.tar.gz | cut -d\" -f4)
sudo tar -C /usr/local/bin -xzf mkbrr_*_linux_arm64.tar.gz mkbrr
```

**Debian/Ubuntu (.deb) — amd64**

```bash
wget $(curl -s https://api.github.com/repos/autobrr/mkbrr/releases/latest | grep download | grep linux_amd64.deb | cut -d\" -f4)
sudo dpkg -i mkbrr_*_linux_amd64.deb
```

**Fedora/CentOS (.rpm) — amd64**

```bash
wget $(curl -s https://api.github.com/repos/autobrr/mkbrr/releases/latest | grep download | grep linux_amd64.rpm | cut -d\" -f4)
sudo rpm -i mkbrr_*_linux_amd64.rpm
```

### macOS

> Ensure the extraction directory (e.g., `/usr/local/bin`) is in your `PATH`.

**Apple Silicon (arm64)**

```bash
wget $(curl -s https://api.github.com/repos/autobrr/mkbrr/releases/latest | grep download | grep darwin_arm64.tar.gz | cut -d\" -f4)
sudo tar -C /usr/local/bin -xzf mkbrr_*_darwin_arm64.tar.gz mkbrr
```

**Intel (x86_64)**

```bash
wget $(curl -s https://api.github.com/repos/autobrr/mkbrr/releases/latest | grep download | grep darwin_x86_64.tar.gz | cut -d\" -f4)
sudo tar -C /usr/local/bin -xzf mkbrr_*_darwin_x86_64.tar.gz mkbrr
```

> **macOS Gatekeeper**: If macOS blocks `mkbrr` from running:
> ```bash
> xattr -d com.apple.quarantine /usr/local/bin/mkbrr
> chmod +x /usr/local/bin/mkbrr
> ```

---

## Package Managers

### Homebrew (macOS/Linux)

```bash
brew tap autobrr/mkbrr
brew install mkbrr
```

### Arch Linux (AUR)

```bash
yay -S mkbrr
```

Or manually:

```bash
git clone https://aur.archlinux.org/mkbrr.git
cd mkbrr
makepkg -si
```

> The AUR version may be outdated. Run `mkbrr update` after installing to get the latest release.

### Alpine Linux

```bash
sudo apk update
sudo apk add mkbrr
```

> The Alpine `edge/testing` version may be outdated. Run `mkbrr update` after installing.

---

## Docker

This is the method used by **mkbrr-wizard**.

Pull the image:

```bash
docker pull ghcr.io/autobrr/mkbrr
```

Tag it for convenience:

```bash
docker tag ghcr.io/autobrr/mkbrr mkbrr
```

Run and mount your input/output directories:

```bash
docker run -v ~/Downloads:/downloads mkbrr mkbrr create /downloads/your-file --output-dir /downloads
```

**Unraid alias example** (with config auto-discovery):

```bash
alias mkbrr='docker run --rm \
  -v /mnt/user/data/complete/Music:/music \
  -v /mnt/user/data/torrentfiles/created:/torrentfiles \
  -v /mnt/user/appdata/mkbrr:/root/.config/mkbrr \
  -w /root/.config/mkbrr \
  ghcr.io/autobrr/mkbrr mkbrr'
```

> Setting `-w` to your config directory inside the container lets mkbrr automatically discover `presets.yaml`.

**Rootless Docker**: If you encounter permission issues, use `--user`:

```bash
docker run --user $(id -u):$(id -g) -v ~/Downloads:/downloads ghcr.io/autobrr/mkbrr mkbrr create /downloads/your-file --output-dir /downloads
```

---

## Go Install

Requires Go 1.23 or later:

```bash
go install github.com/autobrr/mkbrr@latest
```

Ensure `$(go env GOPATH)/bin` is in your `PATH`:

```bash
echo 'export PATH=$PATH:$(go env GOPATH)/bin' >> ~/.zshrc && source ~/.zshrc
```

---

## Verify Installation

```bash
mkbrr version
```

If you see the version number, mkbrr is installed correctly.

---

## Troubleshooting

**Package manager version outdated**: Run `mkbrr update` to fetch the latest binary directly from GitHub.

**Command not found**:

```bash
# For Go installations
echo 'export PATH=$PATH:$(go env GOPATH)/bin' >> ~/.zshrc && source ~/.zshrc

# For binary installations
echo 'export PATH=$PATH:/usr/local/bin' >> ~/.zshrc && source ~/.zshrc
```

**macOS Gatekeeper**:

```bash
xattr -d com.apple.quarantine /usr/local/bin/mkbrr
chmod +x /usr/local/bin/mkbrr
```

---

Need help? Join the [Discord](https://discord.gg/WehFCZxq5B) community or open an issue on [GitHub](https://github.com/autobrr/mkbrr).
