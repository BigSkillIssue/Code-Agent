# syntax=docker/dockerfile:1.7
# The Android emulator Forge's agent uses the products' Android apps on (W61), run by
# forge-host-worker (`run --devices`) on a host with /dev/kvm. Build it once on the host and set
# its name in Forge Web (Admin > Settings > The agent's devices):
#
#   docker build -t forge-android-emulator:34 -f docker/android-emulator.Dockerfile docker
#
# The worker starts it read-only as an unprivileged user in the group of /dev/kvm, with a tmpfs
# at /tmp; this image copies its device to /tmp at start, boots it headless and keeps adb inside,
# where the worker reaches it with `docker exec <container> adb ...`.
FROM eclipse-temurin:17-jdk-jammy

ARG CMDLINE_TOOLS=11076708
ARG API=34
ENV ANDROID_SDK_ROOT=/opt/android-sdk \
    ANDROID_HOME=/opt/android-sdk \
    PATH=/opt/android-sdk/platform-tools:/opt/android-sdk/emulator:/opt/android-sdk/cmdline-tools/latest/bin:$PATH

RUN apt-get update \
 && apt-get install -y --no-install-recommends unzip libpulse0 libgl1 libnss3 libxcomposite1 \
      libxcursor1 libxdamage1 libxi6 libxtst6 libxrandr2 libasound2 libdrm2 libgbm1 \
 && rm -rf /var/lib/apt/lists/* \
 && mkdir -p /opt/android-sdk/cmdline-tools \
 && curl -fsSL -o /tmp/tools.zip \
      "https://dl.google.com/android/repository/commandlinetools-linux-${CMDLINE_TOOLS}_latest.zip" \
 && unzip -q /tmp/tools.zip -d /opt/android-sdk/cmdline-tools \
 && mv /opt/android-sdk/cmdline-tools/cmdline-tools /opt/android-sdk/cmdline-tools/latest \
 && rm /tmp/tools.zip \
 && yes | sdkmanager --licenses > /dev/null \
 && sdkmanager --install "platform-tools" "emulator" "system-images;android-${API};google_apis;x86_64" \
 && echo no | avdmanager create avd -n forge -k "system-images;android-${API};google_apis;x86_64" \
      -d pixel_6 -p /opt/avd/forge.avd \
 && sed -i 's/^hw.keyboard=.*/hw.keyboard=yes/' /opt/avd/forge.avd/config.ini \
 && chmod -R a+rX /opt/android-sdk /opt/avd

COPY --chmod=0755 <<'EOF' /usr/local/bin/forge-emulator
#!/bin/sh
# Copy the device to the writable /tmp, then boot it without a window, sound or snapshots.
set -e
mkdir -p /tmp/avd /tmp/.android
cp -r /opt/avd/forge.avd /tmp/avd/forge.avd
printf 'avd.ini.encoding=UTF-8\npath=/tmp/avd/forge.avd\ntarget=android-%s\n' "$API" > /tmp/avd/forge.ini
export ANDROID_AVD_HOME=/tmp/avd ANDROID_EMULATOR_HOME=/tmp/.android ANDROID_USER_HOME=/tmp/.android
exec emulator -avd forge -no-window -no-audio -no-boot-anim -no-snapshot -no-metrics \
     -gpu swiftshader_indirect -memory 3072 -cores 2
EOF
ENV API=${API}
ENTRYPOINT ["/usr/local/bin/forge-emulator"]
