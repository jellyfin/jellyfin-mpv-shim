# -*- coding: utf-8 -*-
from __future__ import division, absolute_import, print_function, unicode_literals

#################################################################################################


class HTTPException(Exception):
    # Jellyfin HTTP exception
    def __init__(self, status, message):
        self.status = status
        self.message = message
