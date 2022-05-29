from typing import *
from datetime import datetime, timedelta
from dateutil import parser
import time
import arrow
import random
import string
import os
from glob import glob

from youbot import ColorLogger, YoutubeMySqlDatastore, DropboxCloudManager
from .youtube_api import YoutubeApiV3

logger = ColorLogger(logger_name='YoutubeManager', color='cyan')


class YoutubeManager(YoutubeApiV3):
    __slots__ = ('db', 'dbox', 'comments_conf', 'default_sleep_time', 'max_posted_hours', 'api_type',
                 'template_comments', 'log_path', 'upload_logs_every')

    def __init__(self, config: Dict, db_conf: Dict, cloud_conf: Dict, comments_conf: Dict,
                 sleep_time: int, max_posted_hours: int,
                 api_type: str, tag: str, log_path: str):
        global logger
        logger = ColorLogger(logger_name=f'[{tag}] YoutubeManager', color='cyan')
        self.db = YoutubeMySqlDatastore(config=db_conf['config'], tag=tag)
        self.comments_conf = None
        if comments_conf is not None:
            self.comments_conf = comments_conf['config']
        self.dbox = None
        if cloud_conf is not None:
            self.dbox = DropboxCloudManager(config=cloud_conf['config'])
            self.dbox_logs_folder_path = cloud_conf['logs_folder_path']
            self.upload_logs_every = int(cloud_conf['upload_logs_every'])
        elif self.comments_conf is not None:
            if self.comments_conf['type'] == 'dropbox':
                raise YoutubeManagerError("Requested `dropbox` comments type "
                                          "but `cloudstore` config is not set!")
        self.default_sleep_time = sleep_time
        self.max_posted_hours = max_posted_hours
        self.api_type = api_type
        self.template_comments = {}
        if self.api_type == 'simulated':
            self.get_uploads = self.simulate_uploads
        self.log_path = log_path
        super().__init__(config, tag)

    def commenter(self):
        # Initialize
        sleep_time = 0
        loop_cnt = 0
        # Start the main loop
        while True:
            time.sleep(sleep_time)
            # Log upload handling
            if self.dbox is not None:
                loop_cnt += 1
                if loop_cnt > self.upload_logs_every:
                    self.upload_logs()
                    loop_cnt = 0
            # Load necessary data
            self.load_template_comments()
            channel_ids = [channel['channel_id'] for channel in
                           self.db.get_channels()]
            commented_comments, video_links_commented = self.get_comments(channel_ids=channel_ids,
                                                                          n_recent=500)

            latest_videos = self.get_uploads(channels=channel_ids,
                                             max_posted_hours=self.max_posted_hours)
            comments_added = []
            # Sort the videos by the priority of the channels (channel_ids are sorted by priority)
            # and comment in the videos not already commented
            try:
                for video in sorted(latest_videos,
                                    key=lambda _video: channel_ids.index(_video["channel_id"])):
                    video_url = f'https://youtube.com/watch?v={video["id"]}'
                    if video_url not in video_links_commented:
                        comment_text = \
                            self.get_next_template_comment(channel_id=video["channel_id"],
                                                           commented_comments=commented_comments)
                        # self.comment(video_id=video["id"], comment_text=comment_text)
                        # Add the info of the new comment to be added in the DB after this loop
                        comments_added.append((video, video_url, comment_text,
                                               datetime.utcnow().isoformat()))
            except Exception as e:
                error_txt = f"Exception in the main loop of the Commenter:\n{e}"
                logger.error(error_txt)
                sleep_time = self.seconds_until_next_hour()
                logger.error(f"Will sleep until next hour ({sleep_time} seconds)")
            else:
                sleep_time = self.default_sleep_time
            # Save the new comments added in the DB
            try:
                for (video, video_url, comment_text, comment_time) in comments_added:
                    self.db.add_comment(video["channel_id"], video_link=video_url,
                                        comment_text=comment_text, upload_time=video["published_at"])
            except Exception as e:
                error_txt = f"FatalMySQL error while storing comment:\n{e}"
                logger.error(error_txt)
                raise e

    def accumulator(self):
        # Initialize
        sleep_time = 0
        while True:
            try:
                time.sleep(sleep_time)
                # Load recent comments
                recent_commented_links = [comment["video_link"] for comment in
                                          self.db.get_comments(n_recent=200)]
                # Get info for recent comments with YT api
                comments = []
                exceptions = []
                for cnt, link in enumerate(recent_commented_links):
                    try:
                        comments.extend(self.get_video_comments(link))
                    except Exception as e:
                        exceptions.append(e)
                # Update comment data in the DB
                for comment_dict in comments:
                    self.db.update_comment(video_link=comment_dict['url'],
                                           comment_id=comment_dict['comment_id'],
                                           like_cnt=comment_dict['like_count'],
                                           reply_cnt=comment_dict['reply_count'])
                if len(exceptions) > 0:
                    logger.error(f"{len(exceptions)} exceptions occurred! "
                                 f"Will only print  the first one.")
                    raise exceptions[0]
            except Exception as e:
                error_txt = f"Exception in the main loop of the Accumulator:\n{e}"
                logger.error(error_txt)
                sleep_time = self.seconds_until_next_hour()
                logger.error(f"Will sleep until next hour ({sleep_time} seconds)")
            else:
                sleep_time = self.default_sleep_time

    def get_comments(self, n_recent, channel_ids):
        commented_comments = {}
        video_links_commented = []
        for channel_id in channel_ids:
            commented_comments[channel_id] = list(self.db.get_comments(channel_id=channel_id,
                                                                       n_recent=n_recent))
            video_links_commented += [comment['video_link'] for comment in
                                      commented_comments[channel_id]]
        return commented_comments, video_links_commented

    def add_channel(self, channel_id: str = None, username: str = None) -> None:
        if channel_id:
            channel_info = self.get_channel_info_by_id(channel_id)
        elif username:
            channel_info = self.get_channel_info_by_username(username)
        else:
            raise YoutubeManagerError("You should either pass channel id or username "
                                      "to add channel!")
        if channel_info:
            self.db.add_channel(channel_data=channel_info)
            logger.info(f"Channel `{channel_info['username']}` successfully added!")
        else:
            raise YoutubeManagerError("Channel not found!")

    def remove_channel(self, channel_id: str = None, username: str = None) -> None:
        if channel_id:
            self.db.remove_channel_by_id(channel_id)
            logger.info(f"Channel `{channel_id}` successfully removed!")
        elif username:
            self.db.remove_channel_by_username(username)
            logger.info(f"Channel `{username}` successfully removed!")
        else:
            raise YoutubeManagerError("You should either pass channel id or username "
                                      "to remove channel!")

    def refresh_photos(self):
        channel_ids = [channel["channel_id"] for channel in self.db.get_channels()]
        profile_pictures = self.get_profile_pictures(channel_ids)
        for channel_id, picture_url in profile_pictures:
            self.db.update_channel_photo(channel_id, picture_url)

    def set_priority(self, channel_id: str = None, username: str = None, priority: str = None) -> None:
        if channel_id:
            channel_info = self.get_channel_info_by_id(channel_id)
        elif username:
            channel_info = self.get_channel_info_by_username(username)
        else:
            raise YoutubeManagerError("You should either pass channel id or username "
                                      "to add channel!")
        if channel_info:
            self.db.set_priority(channel_data=channel_info, priority=priority)
            logger.info(f"Channel `{channel_info['username']}` priority changed to {priority}!")
        else:
            raise YoutubeManagerError("Channel not found!")

    def list_channels(self) -> None:
        channels = [(row["priority"], row["username"].title(), row["channel_id"],
                     arrow.get(row["added_on"]).humanize(),
                     arrow.get(row["last_commented"]).humanize(),
                     row["channel_photo"]
                     )
                    for row in self.db.get_channels()]

        headers = ['Priority', 'Channel Name', 'Channel ID', 'Added On', 'Last Commented',
                   'Channel Photo']
        self.pretty_print(headers, channels)

    def list_comments(self, n_recent: int = 50, min_likes: int = -1,
                      min_replies: int = -1) -> None:

        comments = [(row["username"].title(), row["comment"],
                     arrow.get(row["comment_time"]).humanize(),
                     row["like_count"], row["reply_count"], row["comment_link"])
                    for row in self.db.get_comments(n_recent, min_likes, min_replies)]

        headers = ['Channel', 'Comment', 'Time', 'Likes', 'Replies', 'Comment URL']
        self.pretty_print(headers, comments)

    def load_template_comments(self):
        if self.comments_conf is None:
            raise YoutubeManagerError("Tried to load template comments "
                                      "but `comments` is not set in the config!")
        # Download files from dropbox
        if self.comments_conf['type'] == 'dropbox':
            # TODO: implement this in the dropbox lib
            if not os.path.exists(self.comments_conf["local_folder_name"]):
                os.makedirs(self.comments_conf["local_folder_name"])
            for file in self.dbox.ls(self.comments_conf['dropbox_folder_name']).keys():
                if file[-4:] == '.txt':
                    self.dbox.download_file(f'{self.comments_conf["dropbox_folder_name"]}/{file}',
                                            f'{self.comments_conf["local_folder_name"]}/{file}')
        # Load comments from files
        if self.comments_conf['type'] in ('local', 'dropbox'):
            base_path = os.path.dirname(os.path.abspath(__file__))
            comments_path = os.path.join(base_path, '../..', self.comments_conf['local_folder_name'],
                                         "*.txt")
            for file in glob(comments_path):
                file_name = file.split('/')[-1][:-4]
                with open(file) as f:
                    self.template_comments[file_name] = [_f.rstrip() for _f in f.readlines()]

    def get_next_template_comment(self, channel_id: str, commented_comments: Dict) -> str:
        """ TODO: Probably much more efficient with numpy or sql. """
        commented_comments = commented_comments[channel_id]
        available_comments = self.template_comments['default'].copy()
        # Build the comments pool
        if channel_id in self.template_comments:
            available_comments += self.template_comments[channel_id]
        # Extract unique comments commented
        unique_com_coms = set(data['comment'] for data in commented_comments)
        new_comments = set(available_comments) - unique_com_coms
        if new_comments:  # If we have new template comments
            comment = next(iter(new_comments))
        else:  # Otherwise, pick the oldest one (with duplicate handling
            comment_dates = {}
            for unique_comment in unique_com_coms:
                comment_dates[unique_comment] = parser.parse('1994-04-30T08:00:00.000000')
                for com_data in commented_comments:
                    if com_data['comment'] == unique_comment:
                        comment_time = parser.parse(com_data['comment_time'])
                        if comment_time > comment_dates[unique_comment]:
                            comment_dates[unique_comment] = parser.parse(com_data['comment_time'])
            comment = [k for k, v in sorted(comment_dates.items(),
                                            key=lambda p: p[1], reverse=False)][0]

        return comment

    def upload_logs(self):
        log_name = self.log_path.split(os.sep)[-1][:-4]
        day = datetime.today().day
        log_name += f'_day{day}.txt'
        upload_path = os.path.join(self.dbox_logs_folder_path, log_name)
        with open(self.log_path, 'rb') as f:
            file_to_upload = f.read()
        self.dbox.upload_file(file_bytes=file_to_upload, upload_path=upload_path)

    def simulate_uploads(self, channels: List, max_posted_hours: int = 2) -> Dict:
        """ Generates new uploads for the specified channels.

        Args:
            channels(list): A list with channel IDs
            max_posted_hours:
        """
        num_videos = random.randint(1, 4)
        channels = [(channel['username'], channel['channel_id']) for channel in
                    self.db.get_channels()]
        for video_ind in range(num_videos):
            vid_id = ''.join(
                random.choices(string.ascii_uppercase + string.ascii_lowercase + string.digits, k=11))
            title_length = random.randint(10, 40)
            vid_title = ''.join(random.choices(string.ascii_lowercase + ' ', k=title_length)).title()
            ch_name, ch_id = random.choice(channels)
            channels.remove((ch_name, ch_id))
            secs = random.randint(1, 59)
            mins = random.randint(1, 59)
            hours = random.randint(1, 59)
            published_at = (datetime.utcnow() - timedelta(seconds=secs,
                                                          minutes=mins,
                                                          hours=hours)).isoformat()
            upload = {'id': vid_id,
                      'published_at': published_at,
                      'title': vid_title,
                      'channel_title': ch_name,
                      'channel_id': ch_id}
            yield upload

    @staticmethod
    def pretty_print(headers: List[str], data: List[Tuple]):
        """Print the provided header and data in a visually pleasing manner

        Args:
            headers: The headers to print
            data: The data rows
        """

        print_str = "\n"
        if len(data) == 0:
            return

        separators = []
        for word in headers:
            separators.append('-' * len(word))

        output = [headers, separators] + data

        col_widths = [0] * len(headers)
        for row in output:
            for idx, column in enumerate(row):
                if len(str(column)) > 100:
                    row[idx] = row[idx][:94] + " (...)"
                if len(str(row[idx])) > col_widths[idx]:
                    col_widths[idx] = len(row[idx])

        for row in output:
            for idx, column in enumerate(row):
                column = str(column)
                print_str += "".join(column.ljust(col_widths[idx])) + "  "
            print_str += '\n'
        logger.info(print_str)

    @staticmethod
    def seconds_until_next_hour() -> int:
        delta = timedelta(hours=1)
        now = datetime.now()
        next_hour = (now + delta).replace(microsecond=0, second=0, minute=2)
        return (next_hour - now).seconds


class YoutubeManagerError(Exception):
    def __init__(self, message):
        super().__init__(message)
