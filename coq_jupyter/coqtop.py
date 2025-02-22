from __future__ import unicode_literals

import pexpect
import re
import xml.etree.ElementTree as ET

from future.utils import raise_with_traceback
from builtins import zip
from future.moves.itertools import zip_longest
from collections import deque
from pexpect.exceptions import ExceptionPexpect
from subprocess import check_output

LANGUAGE_VERSION_PATTERN = re.compile(r'version (\d+(\.\d+)+)')

INIT_COMMAND = '<call val="Init"> <option val="none"/> </call>'
STATUS_COMMAND = '<call val="Status"> <bool val="true"/> </call>'
GOAL_COMMAND = '<call val="Goal"> <unit/> </call>'

ADD_COMMAND_TEMPLATE = """
<call val="Add">
  <pair>
    <pair> <string></string> <int>0</int> </pair>
    <pair> <state_id val="" /> <bool val="false" /> </pair>
  </pair>
</call>
"""

EDIT_AT_COMMAND_TEMPLATE = '<call val="Edit_at"> <state_id val="{0}"/> </call>'

REPLY_PATTERNS = [
    re.compile(r'\<{0}.*?\>.+?\<\/{0}\>'.format(t), re.DOTALL)
    for t in [
        "feedback",
        "value",
        "message" # older versions of coqtop wont wrap 'message' inside 'feedback'
    ]
]

class CoqtopError(Exception): pass

class Coqtop:

    def __init__(self, kernel, coqtop_args):
        try:
            self.log = kernel.log

            # locate coqtop executable
            for cmd in ["coqidetop", "coqtop"]:
                try:
                    banner = check_output([cmd, '--version']).decode('utf-8')
                    break
                except:
                    cmd = None

            if cmd is None:
                raise CoqtopError("Failed to locate 'coqidetop' or 'coqtop' executables")

            version = LANGUAGE_VERSION_PATTERN.search(banner).group(1)
            (parsed_version, version_8_9) = zip(*zip_longest(map(int, version.split(".")), [8, 9], fillvalue=0))

            if cmd == "coqtop" and parsed_version >= version_8_9:
                raise CoqtopError("Failed to locate 'coqidetop' executable ('coqtop' has been found but is insufficient since v8.9)")

            self.cmd = cmd
            self.version = version
            self.banner = banner

            # run coqtop executable
            spawn_args = {
                "echo": False,
                "encoding": "utf-8",
                "codec_errors": "replace"
            }
            if self.cmd == "coqidetop":
                self._coqtop = pexpect.spawn("coqidetop -main-channel stdfds {}".format(coqtop_args), **spawn_args)
            else:
                self._coqtop = pexpect.spawn("coqtop -toploop coqidetop -main-channel stdfds {}".format(coqtop_args), **spawn_args)

            # perform init
            (reply, _) = self._execute_command(INIT_COMMAND)
            self.tip = reply.find("./state_id").get("val")

        except Exception as e:
            raise_with_traceback(CoqtopError("Cause: {}".format(repr(e))))

    def eval(self, code):
        try:
            tip_before = self.tip

            # split code into sentences (headlesssly)
            sentences = code.split(".")
            leftover = sentences[-1]
            sentences = deque(map(lambda s: s + ".", sentences[0:-1]))
            if leftover.strip(" \t\n\r") != "":
                sentences.append(leftover)

            # Attempt to evaluate sentences in code
            code_evaluated = True
            outputs = []
            while len(sentences) > 0:
                sentence = sentences.popleft()

                (add_reply, _) = self._execute_command(self._build_add_command(sentence, self.tip), allow_fail=True)
                (status_reply, out_of_band_status_replies) = self._execute_command(STATUS_COMMAND, allow_fail=True)

                sentence_evaluated = self._is_good(add_reply) and self._is_good(status_reply)
                errors = [
                    self._get_error_content(r)
                    for r in [add_reply, status_reply]
                    if self._has_error(r)
                ]
                out_of_band_status_messsages = [
                    self._get_message_content(r)
                    for r in out_of_band_status_replies
                    if self._is_message(r)
                ]

                if not sentence_evaluated:
                    # In some cases (for example if there is invalid reference) erroneus command
                    # can be accepted by parser, increase coqtop tip and fail late.
                    # To ensure consistent state it is better to roll back to predictable state
                    self.roll_back_to(self.tip)

                if not sentence_evaluated and len(sentences) > 0:
                    # Attempt to fix error by joining erroneus sentence with next one.
                    # This should fix any errors caused by headless splitting
                    # of code into sentences
                    sentences.appendleft(sentence + sentences.popleft())
                    continue

                if not sentence_evaluated and len(sentences) == 0 and self._is_end_of_input_error(add_reply):
                    # It is ok to ignore failute and output generated by evaluating
                    # 'effectively empty' leftover sentence
                    break

                if not sentence_evaluated and len(sentences) == 0:
                    # Upon reaching this state it we can definitely say that there is
                    # an error in cell code
                    code_evaluated = False
                    outputs.extend(errors)
                    break

                self.tip = self._get_nexttip(add_reply)
                outputs.extend(out_of_band_status_messsages)

            if code_evaluated:
                # Get data about theorem being proven
                if self._is_proving(status_reply):
                    outputs.append("Proving: {}".format(self._get_proof_name(status_reply)))

                # Get goal state
                (goal_reply, _) = self._execute_command(GOAL_COMMAND)
                if self._has_goals(goal_reply):
                    outputs.append(self._get_goals_content(goal_reply))
            else:
                # roll back any side effects of code
                self.roll_back_to(tip_before)

            return (code_evaluated, outputs)

        except Exception as e:
            raise_with_traceback(CoqtopError("Cause: {}".format(repr(e))))

    def roll_back_to(self, state_id):
        self._execute_command(self._build_edit_at_command(state_id))
        self.tip = state_id

    def _execute_command(self, command, allow_fail=False):
        self.log.debug("Executing coqtop command: {}".format(repr(command)))
        self._coqtop.send(command + "\n")
        out_of_band_replies = []
        while True:
            self._coqtop.expect(REPLY_PATTERNS)

            if self._coqtop.before.strip(" \t\n\r") != "":
                self.log.warning("Skipping unexpected coqtop output: {}".format(repr(self._coqtop.before)))

            reply = self._parse(self._coqtop.match.group(0))
            self.log.debug("Received coqtop reply: {}".format(ET.tostring(reply)))

            if reply.tag == "value" and not allow_fail and not self._is_good(reply):
                raise CoqtopError("Unexpected reply: {}".format(ET.tostring(reply)))
            elif reply.tag == "value":
                return (reply, out_of_band_replies)
            else:
                out_of_band_replies.append(reply)

    def _parse(self, reply):
        return ET.fromstring(reply.replace("&nbsp;", " "))

    def _get_nexttip(self, reply):
        state_id = reply.find("[@val='good']/pair/pair/union/state_id")
        if state_id is None:
            state_id = reply.find("[@val='good']/pair/state_id")
        return state_id.get("val")

    def _is_good(self, reply):
        return reply.get("val") == "good"

    def _unwrap_error(self, reply):
        return reply.find(".[@val='fail']/richpp/..")

    def _has_error(self, reply):
        return self._unwrap_error(reply) is not None

    def _get_error_content(self, reply):
        # TODO add error context using loc_s, loc_e
        error_content = self._format_richpp(self._unwrap_error(reply).find("./richpp"))
        error_prefix = "Error: "
        if error_content.startswith(error_prefix):
            return error_content
        else:
            return error_prefix + error_content

    def _unwrap_message(self, reply):
        return reply if reply.tag == "message" else reply.find(".//message")

    def _is_message(self, reply):
        return self._unwrap_message(reply) is not None

    def _get_message_content(self, reply):
        message = self._unwrap_message(reply)
        message_content = self._format_richpp(message.find("./richpp"))
        message_level = message.find("./message_level").get("val")
        message_level_prefix = "{}: ".format(message_level.capitalize())

        if message_level == "notice" or message_content.startswith(message_level_prefix):
            return message_content
        else:
            return message_level_prefix + message_content

    def _unwrap_goals(self, reply):
        return reply.find("./option/goals")

    def _has_goals(self, reply):
        return self._unwrap_goals(reply) is not None

    def _get_goals_content(self, reply):
        goals = self._unwrap_goals(reply)
        current_goals = list(goals.find("./list").findall("./goal"))

        if len(current_goals) == 0:
            return "No more subgoals"
        elif len(current_goals) == 1:
            header_content = "1 subgoal"
        else:
            header_content = "{} subgoals".format(len(current_goals))

        hypotheses_content = "\n".join(map(self._format_richpp, current_goals[0].findall("./list/richpp")))

        goals_content = "\n".join(map(
            lambda d: "{}\n{}".format(
                "{}/{} -----------".format(d[0] + 1, len(current_goals))[0:15],
                self._format_richpp(d[1].find("./richpp"))
            ),
            enumerate(current_goals)
        ))

        return "\n\n".join(filter(lambda c: c != "", [header_content, hypotheses_content, goals_content]))

    def _is_proving(self, reply):
        return reply.find(".status/option/string") is not None

    def _get_proof_name(self, reply):
        return reply.find(".status/option/string").text

    def _format_richpp(self, richpp):
        return ET.tostring(richpp, encoding='utf8', method='text').decode('utf8').strip("\n\r")

    def _is_end_of_input_error(self, reply):
        if self._is_good(reply):
            return False
        else:
            error_content = self._get_error_content(reply)
            if "Anomaly" in error_content and "Stm.End_of_input" in error_content:
                return True
            elif "Anomaly" in error_content and 'Invalid_argument("vernac_parse")' in error_content: # for older coqtop versions
                return True
            else:
                return False

    def _build_edit_at_command(self, state_id):
        return EDIT_AT_COMMAND_TEMPLATE.format(state_id)

    def _build_add_command(self, sentence, tip):
        command = ET.fromstring(ADD_COMMAND_TEMPLATE)
        command.find("./pair/pair/string").text = sentence
        command.find("./pair/pair[2]/state_id").set("val", tip)
        return ET.tostring(command, encoding='utf8').decode('utf8')
