FROM eclipse-temurin:21-jre-jammy
ARG LANGUAGETOOL_VERSION=6.6
RUN apt-get update && apt-get install -y --no-install-recommends curl unzip \
    && curl --fail --location --retry 3 "https://languagetool.org/download/LanguageTool-${LANGUAGETOOL_VERSION}.zip" -o /tmp/lt.zip \
    && unzip -q /tmp/lt.zip -d /opt \
    && mv "/opt/LanguageTool-${LANGUAGETOOL_VERSION}" /opt/languagetool \
    && rm /tmp/lt.zip && rm -rf /var/lib/apt/lists/* \
    && useradd --system --uid 10001 languagetool
WORKDIR /opt/languagetool
USER languagetool
EXPOSE 8081
CMD ["java", "-cp", "languagetool-server.jar", "org.languagetool.server.HTTPServer", "--port", "8081", "--public"]
