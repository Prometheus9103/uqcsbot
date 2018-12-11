import argparse
import base64
import json
import os
import random
from datetime import datetime, timezone, timedelta
from functools import partial
from typing import List, Dict, Union, NamedTuple, Optional, Callable, Set

import requests
import sqlalchemy
from sqlalchemy import Column, Integer, String
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker

from uqcsbot import bot, Command
from uqcsbot.api import Channel
from uqcsbot.utils.command_utils import loading_status, UsageSyntaxException

API_URL = "https://opentdb.com/api.php"
CATEGORIES_URL = "https://opentdb.com/api_category.php"

# NamedTuple for use with the data returned from the api
QuestionData = NamedTuple('QuestionData',
                          [('type', str), ('question', str), ('correct_answer', str), ('answers', List[str]),
                           ('is_boolean', bool)])

# Contains information about a reaction and the list of users who used said reaction
ReactionUsers = NamedTuple('ReactionUsers', [('name', str), ('users', Set[str])])

# Customisation options
MIN_SECONDS = 5
MAX_SECONDS = 300

BOOLEAN_REACTS = ['this', 'not-this']  # Format of [ <True>, <False> ]
MULTIPLE_CHOICE_REACTS = ['green_heart', 'yellow_heart', 'heart', 'blue_heart']  # Colours should match CHOICE_COLORS
CHOICE_COLORS = ['#6C9935', '#F3C200', '#B6281E', '#3176EF']

# What arguments to use for the cron job version
CRON_CHANNEL = 'general'
CRON_SECONDS = 600  # Overrides any -s argument below and ignores MAX_SECONDS rule
CRON_ARGUMENTS = ''


@bot.on_command('trivia')
@loading_status
def handle_trivia(command: Command):
    """
        `!trivia [-d <easy|medium|hard>] [-c <CATEGORY>] [-t <multiple|tf>] [-s <N>] [--cats]`
            - Asks a new trivia question
    """
    args = parse_arguments(command.channel_id, command.arg if command.has_arg() else '')

    # End early if the help option was used
    if args.help:
        return

    # Show the leaderboard
    if args.leaderboard:
        show_leaderboard(command.channel_id)
        return

    # Send the possible categories
    if args.cats:
        bot.post_message(command.channel_id, get_categories())
        return

    # TODO: Remove score counts here
    handle_question(command.channel_id, args, score_counts=True)


def parse_arguments(channel: Channel, arg_string: str) -> argparse.Namespace:
    """
    Parses the arguments for the command
    :param command: The command which the handle_trivia function receives
    :return: An argpase Namespace object with the parsed arguments
    """
    parser = argparse.ArgumentParser(prog='!trivia', add_help=False)

    def usage_error(*args, **kwargs):
        raise UsageSyntaxException()

    parser.error = usage_error  # type: ignore
    parser.add_argument('-d', '--difficulty', choices=['easy', 'medium', 'hard'], default='random', type=str.lower,
                        help='The difficulty of the question. (default: %(default)s)')
    parser.add_argument('-c', '--category', default=-1, type=int, help='Specifies a category (default: any)')
    parser.add_argument('-t', '--type', choices=['boolean', 'multiple'], default="random", type=str.lower,
                        help='The type of question. (default: %(default)s)')
    parser.add_argument('-s', '--seconds', default=30, type=int,
                        help='Number of seconds before posting answer (default: %(default)s)')
    parser.add_argument('-l', '--leaderboard', action='store_true',
                        help='Shows the current trivia leaderboard')
    parser.add_argument('--cats', action='store_true', help='Sends a list of valid categories to the user')
    parser.add_argument('-h', '--help', action='store_true', help='Prints this help message')

    args = parser.parse_args(arg_string.split())

    # If the help option was used print the help message to the channel (needs access to the parser to do this)
    if args.help:
        bot.post_message(channel, parser.format_help())

    # Constrain the number of seconds to a reasonable frame
    args.seconds = max(MIN_SECONDS, args.seconds)
    args.seconds = min(args.seconds, MAX_SECONDS)

    return args


def get_categories() -> str:
    """Gets the message to send if the user wants a list of the available categories."""
    http_response = requests.get(CATEGORIES_URL)
    if http_response.status_code != requests.codes.ok:
        return "There was a problem getting the response"

    categories = json.loads(http_response.content)['trivia_categories']

    # Construct pretty results to print in a code block to avoid a large spammy message
    pretty_results = '```Use the id to specify a specific category \n\nID  Name\n'

    for category in categories:
        pretty_results += f'{category["id"]:<4d}{category["name"]}\n'

    pretty_results += '```'

    return pretty_results


def handle_question(channel: Channel, args: argparse.Namespace, score_counts: bool = False):
    """
    Handles getting a question and posting it to the channel as well as scheduling the answer.
    Returns the reaction string for the correct answer.
    """
    question_data = get_question_data(channel, args)

    if question_data is None:
        return

    post_timestamp = post_question(channel, question_data)

    # Get the answer message
    if question_data.is_boolean:
        answer_text = f':{BOOLEAN_REACTS[0]}:' if question_data.correct_answer == 'True' else f':{BOOLEAN_REACTS[1]}:'
    else:
        answer_text = question_data.correct_answer

    answer_message = f'The answer to the question *{question_data.question}* is: *{answer_text}*'

    # Schedule the answer to be posted after the specified number of seconds has passed
    post_answer = partial(bot.post_message, channel, answer_message)
    if score_counts:
        def post_answer_update_score():
            correct_reaction = get_correct_reaction(question_data)
            update_leaderboard(channel, post_timestamp, correct_reaction)
            post_answer()

        schedule_action(post_answer_update_score, args.seconds)
    else:
        schedule_action(post_answer, args.seconds)


def get_question_data(channel: Channel, args: argparse.Namespace) -> Optional[QuestionData]:
    """
    Attempts to get a question from teh api using the specified arguments.
    Returns the dictionary object for the question on success and None on failure (after posting an error message).
    """
    # Base64 to help with encoding the message for slack
    params: Dict[str, Union[int, str]] = {'amount': 1, 'encode': 'base64'}

    # Add in any explicitly specified arguments
    if args.category != -1:
        params['category'] = args.category

    if args.difficulty != 'random':
        params['difficulty'] = args.difficulty

    if args.type != 'random':
        params['type'] = args.type

    # Get the response and check that it is valid
    http_response = requests.get(API_URL, params=params)
    if http_response.status_code != requests.codes.ok:
        bot.post_message(channel, "There was a problem getting the response")
        return None

    # Check the response codes and post a useful message in the case of an error
    response_content = json.loads(http_response.content)
    if response_content['response_code'] == 2:
        bot.post_message(channel, "Invalid category id. Try !trivia --cats for a list of valid categories.")
        return None
    elif response_content['response_code'] != 0:
        bot.post_message(channel, "No results were returned")
        return None

    question_data = response_content['results'][0]

    # Get the type of question and make the NamedTuple container for the data
    is_boolean = len(question_data['incorrect_answers']) == 1
    answers = [question_data['correct_answer']] + question_data['incorrect_answers']

    # Delete the ones we don't need
    del question_data['category']
    del question_data['difficulty']
    del question_data['incorrect_answers']

    # Decode the ones we want. The base 64 decoding ensures that the formatting works properly with slack.
    question_data['question'] = decode_b64(question_data['question'])
    question_data['correct_answer'] = decode_b64(question_data['correct_answer'])
    answers = [decode_b64(ans) for ans in answers]

    question_data = QuestionData(**question_data, is_boolean=is_boolean, answers=answers)

    # Shuffle the answers
    random.shuffle(question_data.answers)

    return question_data


def post_question(channel: Channel, question_data: QuestionData) -> float:
    """
    Posts the question from the given QuestionData along with the possible answers list if applicable.
    Also creates the answer reacts.
    Returns the timestamp of the posted message.
    """
    # Post the question and get the timestamp for the reactions (asterisks bold it)
    message_ts = bot.post_message(channel, f'*{question_data.question}*')['ts']

    # Print the questions (if multiple choice) and add the answer reactions
    reactions = BOOLEAN_REACTS if question_data.is_boolean else MULTIPLE_CHOICE_REACTS

    if not question_data.is_boolean:
        message_ts = post_possible_answers(channel, question_data.answers)

    for reaction in reactions:
        bot.api.reactions.add(name=reaction, channel=channel, timestamp=message_ts)

    return message_ts


def decode_b64(encoded: str) -> str:
    """Takes a base64 encoded string. Returns the decoded version to utf-8."""
    return base64.b64decode(encoded).decode('utf-8')


def get_correct_reaction(question_data: QuestionData):
    """Returns the reaction that matches with the correct answer"""
    if (question_data.is_boolean):
        correct_reaction = BOOLEAN_REACTS[0] if question_data.correct_answer == 'True' else BOOLEAN_REACTS[1]
    else:
        correct_reaction = MULTIPLE_CHOICE_REACTS[question_data.answers.index(question_data.correct_answer)]

    return correct_reaction


def post_possible_answers(channel: Channel, answers: List[str]) -> float:
    """
    Posts the possible answers for a multiple choice question in a nice way.
    Returns the timestamp of the message to allow reacting to it.
    """
    attachments = []
    for col, answer in zip(CHOICE_COLORS, answers):
        ans_att = {'text': answer, 'color': col}
        attachments.append(ans_att)

    return bot.post_message(channel, '', attachments=attachments)['ts']


def schedule_action(action: Callable, secs: int):
    """Schedules the supplied action to be called once in the given number of seconds."""
    end_date = datetime.now(timezone(timedelta(hours=10))) + timedelta(seconds=secs + 1)

    bot._scheduler.add_job(action, 'interval', seconds=secs, end_date=end_date)


@bot.on_schedule('cron', hour=12, minute=0, timezone='Australia/Brisbane')
def trivia_cron_job():
    """Adds a job that displays a random question to general at lunch time"""
    channel = bot.channels.get(CRON_CHANNEL).id

    # Get arguments and update the seconds
    args = parse_arguments(channel, CRON_ARGUMENTS)
    args.seconds = CRON_SECONDS

    # Get and post the actual question
    handle_question(channel, args, score_counts=True)
    bot.post_message(channel, f'Answer in {CRON_SECONDS//60} minutes')


# Define the mapping for the leaderboard
Base = sqlalchemy.ext.declarative.declarative_base()


class UserScore(Base):
    """
    Class for use with sqlalchemy for the trivia scoreboard
    """
    __tablename__ = 'trivia_leaderboard'
    id = Column(Integer, primary_key=True)
    user_id = Column(String, unique=True)
    score = Column(Integer)

    def __init__(self, user_id: str, score: int):
        self.user_id = user_id
        self.score = score


def update_leaderboard(channel: Channel, ts: float, correct_reaction: str):
    """
    Updates the leaderboard by checking the message in the given channel with the given timestamp and seeing
    which users chose the given correct_reaction.
    If a user has tried to cheat the system by selecting all reactions they will get no points
    """
    reaction_query = bot.api.reactions.get(channel=channel, timestamp=ts)

    if not reaction_query['ok'] or reaction_query['type'] != 'message':
        bot.post_message(channel, 'There was a problem updating the scores')
        return

    reactions = reaction_query['message']['reactions']

    # Convert the dictionary of reactions to a list of ReactionUsers and then get the correct users
    reaction_users = [ReactionUsers(react["name"], set(react["users"])) for react in reactions]
    correct_users = get_correct_users(reaction_users, correct_reaction)

    # Update the database
    # TODO: Don't do this every time (store engine on bot?)
    engine = sqlalchemy.create_engine(os.environ.get("PG_DATABASE_URL"))

    Session = sessionmaker(bind=engine)
    session = Session()

    # Update the users already in the brain (db)
    existing_users = session.query(UserScore).filter(UserScore.user_id.in_(correct_users)).all()

    for user in existing_users:
        user.score += 1

    # Add a row for all of the new users and set their score to 1
    existing_user_ids = set(user.user_id for user in existing_users)
    session.add_all(UserScore(user_id, 1) for user_id in (correct_users - existing_user_ids))

    session.commit()


def get_correct_users(reactions: List[ReactionUsers], correct_reaction: str) -> Set[str]:
    """
    Get a set of all of the user_ids that answered the question with the correct_reaction.
    Excludes anyone who answered with more than one of the valid reactions.
    """

    # Get the list of user ids that answered correctly
    correct_users = None
    for reaction in reactions:
        if reaction.name == correct_reaction:
            correct_users = reaction.users
            break

    if correct_users is None:
        return set()

    # Disqualify them if they answered more than one of the applicable reactions
    is_boolean = correct_reaction == BOOLEAN_REACTS[0] or correct_reaction == BOOLEAN_REACTS[1]
    valid_reactions = BOOLEAN_REACTS if is_boolean else MULTIPLE_CHOICE_REACTS

    for reaction in reactions:
        if reaction.name != correct_reaction and reaction.name in valid_reactions:
            correct_users = correct_users - reaction.users

    return set(correct_users)


def show_leaderboard(channel: Channel):
    """Posts the trivia leaderboard to the given channel"""
    # TODO: Don't do this every time (store engine on bot?)
    engine = sqlalchemy.create_engine(os.environ.get("PG_DATABASE_URL"))

    Session = sessionmaker(bind=engine)
    session = Session()

    # Get the display_names and scores for each user
    user_scores = session.query(UserScore).all()
    display_names = [bot.users.get(user.user_id).display_name for user in user_scores]
    scores = [user.score for user in user_scores]
    longest_name = max(len(max(display_names, key=len)), 4)  # max(x, 4) to account for rare case of all names < 4

    # Construct the message
    message = '```\n{: <{}}  |  Score\n'.format('Name', longest_name)
    message += '-' * (len(message) - 5) + '\n'

    for (name, score) in zip(display_names, scores):
        message += f'{name: <{longest_name}}  |  {score}\n'

    message += '```'

    bot.post_message(channel, message)
