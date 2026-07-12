BOT_NAME = "dgsearch"
SPIDER_MODULES = ["dgsearch.spiders"]
NEWSPIDER_MODULE = "dgsearch.spiders"

# The regional JSON loader is rejected by Scrapy's robots middleware. Operators
# must review the site's current policies before running this collector.
ROBOTSTXT_OBEY = False
USER_AGENT = "dgsearch/0.1 (+https://github.com/hongseok-lee/dgsearch)"

CONCURRENT_REQUESTS = 16
CONCURRENT_REQUESTS_PER_DOMAIN = 2
DOWNLOAD_DELAY = 1.0
RANDOMIZE_DOWNLOAD_DELAY = True

AUTOTHROTTLE_ENABLED = True
AUTOTHROTTLE_START_DELAY = 1.0
AUTOTHROTTLE_MAX_DELAY = 120.0
AUTOTHROTTLE_TARGET_CONCURRENCY = 2.0

ADAPTIVE_CONCURRENCY_MAX = 16
ADAPTIVE_CONCURRENCY_WINDOW = 40
ADAPTIVE_CONCURRENCY_MIN_SAMPLES = 20
ADAPTIVE_CONCURRENCY_FAILURE_RATE = 0.15
ADAPTIVE_CONCURRENCY_RECOVERY_RATE = 0.025

RETRY_ENABLED = True
RETRY_TIMES = 6
RETRY_HTTP_CODES = [408, 425, 429, 500, 502, 503, 504]

HTTPCACHE_ENABLED = True
HTTPCACHE_EXPIRATION_SECS = 86400
HTTPCACHE_IGNORE_HTTP_CODES = [403, 429, 500, 502, 503, 504]

DOWNLOADER_MIDDLEWARES = {
    # Run before Scrapy's RetryMiddleware sees the response.
    "dgsearch.middlewares.RateLimitMiddleware": 560,
}

FEEDS = {
    "output/results.jsonl": {
        "format": "jsonlines",
        "encoding": "utf-8",
        "overwrite": True,
    }
}

LOG_LEVEL = "INFO"
TELNETCONSOLE_ENABLED = False
