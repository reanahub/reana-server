# -*- coding: utf-8 -*-
#
# This file is part of REANA.
# Copyright (C) 2022 CERN.
#
# REANA is free software; you can redistribute it and/or modify it
# under the terms of the MIT License; see LICENSE file for more details.

"""REANA Admin MessageConsumer."""

import json
from enum import Enum, auto
from typing import List, Optional

import click

from reana_commons.consumer import BaseConsumer


class UserDecision(Enum):
    """Possible user decisions in interactive consumer mode."""

    DELETE_MESSAGE = auto()
    KEEP_MESSAGE = auto()
    STOP_CONSUMER = auto()


class MessageConsumer(BaseConsumer):
    """Consumer responsible for cleaning queues."""

    def __init__(
        self,
        queue_name: str,
        key: Optional[str],
        values_to_delete: List[str],
        is_interactive: bool = False,
        **kwargs,
    ):
        """Initialise the class."""
        super(MessageConsumer, self).__init__(queue=queue_name, **kwargs)
        self.key = key
        self.values_to_delete = values_to_delete
        self.is_interactive = is_interactive

        click.secho(
            "MessageConsumer initialized with the following settings: \n"
            f"  - queue_name: {queue_name}\n"
            f"  - message_key: {self.key}\n"
            f"  - values_to_delete: {self.values_to_delete}\n"
            f"  - interactive: {self.is_interactive}"
        )

    def get_consumers(self, Consumer, channel):
        """Implement providing kombu.Consumers with queues/callbacks."""
        return [
            Consumer(
                queues=self.queue,
                callbacks=[self.on_message],
                accept=[self.message_default_format],
                prefetch_count=1,
            )
        ]

    def on_consume_ready(self, connection, channel, consumers, **kwargs):  # noqa: D102
        click.secho(f"Starting consuming the {self.queue.name} queue", fg="green")
        click.secho("If you want to stop the consumer, use Ctrl+C.\n", fg="yellow")

    def on_consume_end(self, connection, channel):  # noqa: D102
        click.secho(f"Finished consuming the {self.queue.name} queue", fg="green")

    @staticmethod
    def ask_user() -> UserDecision:
        """Prompt user to decide what to do next."""
        while True:
            answer = input(
                "Delete the above message? (Y - Yes, N - No, S - Stop consuming): "
            )
            if answer.lower() in ["yes", "y"]:
                return UserDecision.DELETE_MESSAGE
            elif answer.lower() in ["no", "n"]:
                return UserDecision.KEEP_MESSAGE
            elif answer.lower() in ["stop", "s"]:
                return UserDecision.STOP_CONSUMER
            else:
                click.secho("Please, provide correct input.", fg="red")

    def on_message(self, body, message):
        """Remove selected messages."""
        msg_body = json.loads(body)
        click.secho(
            f"New message received: \n{json.dumps(msg_body, sort_keys=True, indent=4)}"
        )

        decision = UserDecision.KEEP_MESSAGE
        should_filter = self.key is not None

        if should_filter:
            value = msg_body.get(self.key, "")
            if value in self.values_to_delete:
                click.secho(
                    f"Message with searched value {value} for {self.key} key is found."
                )
                decision = UserDecision.DELETE_MESSAGE
                if self.is_interactive:
                    decision = self.ask_user()
        elif self.is_interactive:
            decision = self.ask_user()

        if decision == UserDecision.DELETE_MESSAGE:
            message.ack()
            click.secho("Message is ACK and removed from the queue.")
        elif decision == UserDecision.KEEP_MESSAGE:
            message.reject(requeue=True)
            click.secho("Message is ignored and re-queued.")
        elif decision == UserDecision.STOP_CONSUMER:
            self.should_stop = True


class CollectingConsumer(BaseConsumer):
    """Consumer responsible for collecting messages."""

    def __init__(
        self,
        queue_name: str,
        key: Optional[str],
        values_to_collect: List[str],
        **kwargs,
    ):
        """Initialise the class."""
        super(CollectingConsumer, self).__init__(queue=queue_name, **kwargs)
        self.key = key
        self.values_to_collect = values_to_collect
        self.messages = dict()

    def get_consumers(self, Consumer, channel):
        """Implement providing kombu.Consumers with queues/callbacks."""
        return [
            Consumer(
                queues=self.queue,
                callbacks=[self.on_message],
                accept=[self.message_default_format],
                prefetch_count=1,
            )
        ]

    def on_consume_ready(self, connection, channel, consumers, **kwargs):
        """Run the method when consumer is ready, but before starting consuming."""
        bound_queue = self.queue(channel)
        _, msg_count, _ = bound_queue.queue_declare(passive=True)

        if msg_count == 0:
            self.should_stop = True

    def on_iteration(self):
        """Run the method before each message."""
        bound_queue = self.queue(self.connection.channel())
        _, msg_count, _ = bound_queue.queue_declare(passive=True)

        if msg_count == 0:
            self.should_stop = True

    def on_message(self, body, message):
        """Collect messages."""
        msg_body = json.loads(body)

        value = msg_body.get(self.key, "")
        if value in self.values_to_collect:
            self.messages[value] = msg_body

        message.reject(requeue=True)
