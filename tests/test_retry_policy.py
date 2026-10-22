import asyncio
import time
import unittest
from unittest.mock import patch

from services.retry_policy import (AmbiguousPublishStateError, RetryPolicy,
                                   classify_error, run_with_bounded_retry,
                                   retry_delay_seconds, TransientError,
                                   PermanentError)


class RetryPolicyTests(unittest.IsolatedAsyncioTestCase):
    async def test_retry_transient_with_capped_backoff(self):
        attempts = []
        sleeps = []

        async def operation():
            attempts.append(len(attempts) + 1)
            if len(attempts) < 3:
                raise TransientError('connection reset by peer')
            return 'ok'

        async def async_sleep(delay):
            sleeps.append(delay)

        policy = RetryPolicy(max_attempts=3, base_delay_seconds=0.1,
                             max_delay_seconds=0.2, jitter_seconds=0.0)
        result = await run_with_bounded_retry(operation, policy=policy,
                                        sleep_fn=async_sleep,
                                        random_fn=lambda: 0.0)
        self.assertEqual(result, 'ok')
        self.assertEqual(len(attempts), 3)
        # base=0.1, attempt2 -> 0.1, attempt3 -> 0.2 (capped)
        self.assertEqual(sleeps, [0.1, 0.2])

    async def test_retry_non_transient_not_retried(self):
        attempts = []
        sleeps = []

        async def operation():
            attempts.append(1)
            raise PermanentError('selector contract changed')

        async def async_sleep(delay):
            sleeps.append(delay)

        with self.assertRaises(PermanentError):
            await run_with_bounded_retry(operation, sleep_fn=async_sleep)
        self.assertEqual(len(attempts), 1)
        self.assertEqual(sleeps, [])

    async def test_ambiguous_publish_not_retried(self):
        attempts = []

        async def operation():
            attempts.append(1)
            raise AmbiguousPublishStateError('submit unknown')

        async def async_sleep(delay):
            pass

        with self.assertRaises(AmbiguousPublishStateError):
            await run_with_bounded_retry(operation, sleep_fn=async_sleep)
        self.assertEqual(len(attempts), 1)
        self.assertEqual(classify_error(AmbiguousPublishStateError('x')),
                         'publish_ambiguous')

    def test_classify_error_markers(self):
        self.assertEqual(classify_error(TransientError('net::err_connection_reset')),
                         'transient')
        self.assertEqual(classify_error(PermanentError('checkpoint required')),
                         'permanent')
        self.assertEqual(classify_error(ValueError('selector changed')),
                         'unknown')
        self.assertEqual(classify_error(Exception('không tìm thấy nút đăng')),
                         'permanent')
        self.assertEqual(classify_error(Exception('quá thời gian')),
                         'transient')

    def test_delay_capped_and_jitter(self):
        policy = RetryPolicy(base_delay_seconds=1.0, max_delay_seconds=2.0,
                             jitter_seconds=0.0)
        self.assertAlmostEqual(retry_delay_seconds(policy, 1), 1.0)
        self.assertAlmostEqual(retry_delay_seconds(policy, 2), 2.0)
        self.assertAlmostEqual(retry_delay_seconds(policy, 3), 2.0)  # capped