# Hugging Face Space image for the Alcopa deadline watcher.
#
# Playwright's own image already carries Chromium and every system library it
# needs — building Chrome onto a bare python image is the usual way this fails.
FROM mcr.microsoft.com/playwright/python:v1.48.0-jammy

# Spaces run the container as uid 1000 and the rest of the filesystem is not
# writable, so everything this process touches has to live under its own home.
RUN useradd -m -u 1000 user
RUN apt-get update -qq && apt-get install -y -qq git && rm -rf /var/lib/apt/lists/*

USER user
ENV HOME=/home/user \
    PATH=/home/user/.local/bin:$PATH \
    ALCOPA_BROWSER=playwright \
    PYTHONUNBUFFERED=1 \
    TZ=Europe/Paris
WORKDIR $HOME/app

RUN pip install --no-cache-dir --user playwright==1.48.0 huggingface_hub

# The scraper is cloned rather than copied in, so this Space and the VPS path
# run byte-identical code from one place. space_app.py pulls again at every
# start, so a fix reaches the watcher on a restart, without a rebuild.
RUN git clone --depth 1 https://github.com/wancea23/EU-Car-Bidding-Scrapper.git repo

COPY --chown=user space_app.py .

# Spaces route public traffic to 7860 and nothing else.
EXPOSE 7860
CMD ["python", "space_app.py"]
