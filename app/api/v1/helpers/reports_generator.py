"""
DRS Reports generator package.
SPDX-License-Identifier: BSD-4-Clause-Clear
Copyright (c) 2018-2019 Qualcomm Technologies, Inc.
All rights reserved.
Redistribution and use in source and binary forms, with or without modification, are permitted (subject to the limitations in the disclaimer below) provided that the following conditions are met:
    Redistributions of source code must retain the above copyright notice, this list of conditions and the following disclaimer.
    Redistributions in binary form must reproduce the above copyright notice, this list of conditions and the following disclaimer in the documentation and/or other materials provided with the distribution.
    All advertising materials mentioning features or use of this software, or any deployment of this software, or documentation accompanying any distribution of this software, must display the trademark/logo as per the details provided here: https://www.qualcomm.com/documents/dirbs-logo-and-brand-guidelines
    Neither the name of Qualcomm Technologies, Inc. nor the names of its contributors may be used to endorse or promote products derived from this software without specific prior written permission.
SPDX-License-Identifier: ZLIB-ACKNOWLEDGEMENT
Copyright (c) 2018-2019 Qualcomm Technologies, Inc.
This software is provided 'as-is', without any express or implied warranty. In no event will the authors be held liable for any damages arising from the use of this software.
Permission is granted to anyone to use this software for any purpose, including commercial applications, and to alter it and redistribute it freely, subject to the following restrictions:
    The origin of this software must not be misrepresented; you must not claim that you wrote the original software. If you use this software in a product, an acknowledgment is required by displaying the trademark/logo as per the details provided here: https://www.qualcomm.com/documents/dirbs-logo-and-brand-guidelines
    Altered source versions must be plainly marked as such, and must not be misrepresented as being the original software.
    This notice may not be removed or altered from any source distribution.
NO EXPRESS OR IMPLIED LICENSES TO ANY PARTY'S PATENT RIGHTS ARE GRANTED BY THIS LICENSE. THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS" AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE ARE DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT HOLDER OR CONTRIBUTORS BE LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE OF THIS SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.
"""
import os
from app import session, celery, app
from requests import ConnectionError
from app.api.v1.helpers.error_handlers import *

from threading import Thread
from math import ceil

import pandas as pd
import uuid
import json


class BulkCommonResources:  # pragma: no cover
    """Common resources for bulk request."""

    @staticmethod
    @celery.task
    def get_summary(imeis_list, tracking_id):
        """Celery task for bulk request processing."""
        try:
            imeis_chunks = BulkCommonResources.chunked_data(imeis_list)
            records = BulkCommonResources.start_threads(imeis_list=imeis_chunks)
            # send records for summary generation
            response = BulkCommonResources.build_drs_summary(records, tracking_id)
            return response
        except Exception as e:
            raise e

    @staticmethod
    def chunked_data(imeis_list):
        """Divide IMEIs into batches of 1000 and chunks for multi threading."""
        try:
            if imeis_list:
                imeis_list = list(imeis_list[i:i + 1000] for i in
                                  range(0, len(imeis_list), 1000))
                chunksize = int(ceil(len(imeis_list) / 10))
                imeis_list = list(imeis_list[i:i + chunksize] for i in range(0, len(imeis_list), chunksize))
                return imeis_list
            return imeis_list
        except Exception as e:
            raise e

    @staticmethod
    def start_threads(imeis_list):
        """Process IMEIs simultaneously by starting multiple threads at a time."""
        thread_list = []
        records = []
        unprocessed_imeis = []
        for imei in imeis_list:
            thread_list.append(Thread(target=BulkCommonResources.get_records, args=(imei, records, unprocessed_imeis)))

        # start threads for all imei chunks
        for x in thread_list:
            x.start()

        # stop all threads on completion
        for t in thread_list:
            t.join()

        if unprocessed_imeis:
            records, unprocessed_imeis = BulkCommonResources.retry(records, unprocessed_imeis)

        return records

    # get records from core system
    @staticmethod
    def get_records(imeis, records, unprocessed_imeis):
        """Compile IMEIs batch responses from DIRBS core system."""
        try:
            while imeis:
                imei = imeis.pop(-1)  # pop the last item from queue
                try:
                    if imei:
                        batch_req = {
                            "imeis": imei
                        }
                        headers = {'content-type': 'application/json', 'charset': 'utf-8', 'keep_alive': 'false'}
                        app.logger.info('{}/imei-batch'.format(app.config['CORE_BASE_URL']))
                        imei_response = session.post('{}/imei-batch'.format(app.config['CORE_BASE_URL']),
                                                     data=json.dumps(batch_req),
                                                     headers=headers)  # dirbs core batch api call
                        if imei_response.status_code == 200:
                            imei_response = imei_response.json()
                            records.extend(imei_response['results'])
                        else:
                            app.logger.info("imei batch failed due to status other than 200")
                            unprocessed_imeis.append(imei)  # in case of connection error append imei count to unprocessed IMEIs list
                    else:
                        continue
                except (ConnectionError, Exception) as e:
                    unprocessed_imeis.append(imei)  # in case of connection error append imei count to unprocessed IMEIs list
                    app.logger.exception(e)
        except Exception as error:
            raise error

    @staticmethod
    def retry(records, unprocessed_imeis):
        """Retry failed IMEI batches."""
        retry = 10

        while retry and len(unprocessed_imeis) > 0:
            threads = []
            retry = retry - 1
            imeis_list = unprocessed_imeis
            unprocessed_imeis = []
            chunksize = int(ceil(len(imeis_list) / 10))
            imeis_list = list(imeis_list[i:i + chunksize] for i in
                              range(0, len(imeis_list), chunksize))  # make 100 chunks for 1 million imeis
            for imeis in imeis_list:
                threads.append(Thread(target=BulkCommonResources.get_records, args=(imeis, records, unprocessed_imeis)))
            for x in threads:
                x.start()

            for t in threads:
                t.join()

        return records, unprocessed_imeis

    @staticmethod
    def build_drs_summary(records, tracking_id):
        """Generate summary for DRS bulk records."""
        try:
            response = {}
            if records:
                result = pd.DataFrame(records)  # main dataframe for results

                stolen_list = pd.DataFrame(list(result['stolen_status']))   # dataframe for stolen status
                pending_stolen_count = len(stolen_list.loc[stolen_list['provisional_only']==True])

                stolen = len(stolen_list.loc[stolen_list['provisional_only']==False])

                count_per_condition = {}

                realtime = pd.DataFrame(list(result['realtime_checks']))  # dataframe of realtime checks
                seen_on_network = len(realtime.loc[realtime['ever_observed_on_network']==True])

                blocking_condition = pd.DataFrame(i['blocking_conditions'] for i in result['classification_state'] if i['blocking_conditions'])  # dataframe for blocking conditions

                info_condition = pd.DataFrame(i['informative_conditions'] for i in result['classification_state'] if i['informative_conditions'])  # dataframe for informative conditions

                #  IMEI count per blocking condition
                count_per_condition, block = BulkCommonResources.count_condition(count=count_per_condition, conditions=blocking_condition)

                # IMEI count per informative condition
                count_per_condition, info = BulkCommonResources.count_condition(count=count_per_condition, conditions=info_condition)

                # processing compliant status for all IMEIs
                data = BulkCommonResources.generate_drs_compliant_report(records, tracking_id)

                # summary for bulk verify IMEI
                response['provisional_stolen'] = pending_stolen_count
                response['verified_imei'] = len(records)
                response['count_per_condition'] = count_per_condition
                response['non_complaint'] = data['non_compliant']
                response['complaint'] = data['compliant']
                response['provisional_non_compliant'] = data['provisionally_non_compliant']
                response['provisional_compliant'] = data['provisionally_compliant']
                response['seen_on_network'] = seen_on_network
                response['stolen'] = stolen
                response['compliant_report_name'] = data['filename']
            return response
        except Exception as e:
            raise e

    # generate compliant report and count non compliant IMEIs
    @staticmethod
    def generate_drs_compliant_report(records, tracking_id):
        """Return non compliant report for DRS bulk request."""
        non_compliant = 0
        compliant = 0
        provisionally_compliant = 0
        provisionally_non_compliant = 0
        complaint_report = []
        for key in records:
            status = BulkCommonResources.compliance_status(resp=key, status_type="bulk", imei=key['imei_norm'])
            status['stolen_status'] = "Pending Stolen Verification" if key['stolen_status'][
                'provisional_only'] else "Not Stolen" if key['stolen_status']['provisional_only'] is None else "Stolen"
            status['seen_on_network'] = key['realtime_checks']['ever_observed_on_network']
            complaint_report.append(status)
            if "Provisionally Compliant" in status['status']:
                provisionally_compliant += 1
            elif "Provisionally non compliant" in status['status']:
                provisionally_non_compliant += 1
            elif status['status'] == "Compliant (Active)" or status['status'] == "Compliant (Inactive)":
                compliant += 1
            elif status['status'] == "Non compliant":
                non_compliant += 1

        complaint_report = pd.DataFrame(complaint_report)  # dataframe of compliant report
        report_name = 'compliant_report' + str(uuid.uuid4()) + '.tsv'
        report_path = os.path.join(app.config['DRS_UPLOADS'], '{0}'.format(tracking_id))
        complaint_report.to_csv(os.path.join(report_path, report_name), sep='\t')

        del_columns = ['block_date', 'seen_on_network', 'stolen_status']
        restricted_report = complaint_report.drop(del_columns, axis=1, errors='ignore')
        user_report_name = 'user_report-{}'.format(report_name)
        restricted_report.to_csv(os.path.join(report_path, user_report_name), sep='\t')

        data = {
            "non_compliant": non_compliant,
            "compliant": compliant,
            "provisionally_non_compliant": provisionally_non_compliant,
            "provisionally_compliant": provisionally_compliant,
            "filename": report_name,
            "user_report_name": user_report_name
        }
        return data

    # count per condition classification state
    @staticmethod
    def count_condition(conditions, count):
        """Helper functions to generate summary, returns IMEI count per condition."""
        condition = []
        transponsed = conditions.transpose()
        for c in transponsed:
            cond = {}
            for i in transponsed[c]:
                cond[i['condition_name']] = i['condition_met']  # serialize conditions in list of dictionaries
            condition.append(cond)
        condition = pd.DataFrame(condition)
        for key in condition:  # iterate over list
            count[key] = len(condition[condition[key]])  # count meeting conditions
        return count, condition

    @staticmethod
    def compliance_status(resp, status_type, imei=None):
        """Evaluate IMEIs to be compliant/non complaint."""

        try:
            status = {}
            seen_with = resp['realtime_checks']['ever_observed_on_network']
            blocking_conditions = resp['classification_state']['blocking_conditions']
            stolen_status = resp['stolen_status']['provisional_only']
            reg_status = resp['registration_status']['provisional_only']
            block_date = resp.get('block_date', 'N/A')

            if reg_status:  # device's registration request is pending
                if stolen_status:  # device's stolen request pending
                    status = BulkCommonResources.populate_status(status, 'Provisionally non compliant', status_type, blocking_conditions, ['Your device is stolen report is pending'], imei=imei, block_date=block_date)
                elif stolen_status is False:  # device is stolen
                    status = BulkCommonResources.populate_status(status, 'Non compliant', status_type, blocking_conditions, ['Your device is stolen'], imei=imei, block_date=block_date)
                else:  # device is not stolen
                    status = BulkCommonResources.populate_status(status, 'Provisionally Compliant', status_type)
            elif reg_status is None:  # device is not registered
                status = BulkCommonResources.populate_status(status, 'Non compliant', status_type, blocking_conditions, ['Your device is not registered'], imei=imei, block_date=block_date)
            else:  # device is registered
                if stolen_status:  # stolen request is pending
                    status = BulkCommonResources.populate_status(status, 'Provisionally non compliant', status_type, blocking_conditions, ['Your device stolen report is pending'], imei=imei, block_date=block_date)
                elif stolen_status is None:  # device is not stolen
                    status = BulkCommonResources.populate_status(status, 'Compliant', status_type, seen_with=seen_with)
                else:  # stolen device
                    status = BulkCommonResources.populate_status(status, 'Non compliant', status_type, blocking_conditions, ['Your device is stolen'], imei=imei, block_date=block_date)
            return status
        except Exception as error:
            raise error

    @staticmethod
    def populate_status(resp, status, status_type, blocking_condition=None, reason_list=None, imei=None, block_date=None, seen_with=None):
        """Return compliant status of an IMEI."""

        try:
            if status == 'Compliant' or status == 'Provisionally Compliant':
                if seen_with:
                    resp['status'] = status + ' (Active)'
                else:
                    resp['status'] = status + ' (Inactive)'

                if status_type == "bulk":
                    return resp
                else:
                    return {"compliant": resp}
            else:
                resp['status'] = status
                resp['block_date'] = block_date
                if status_type == "basic":
                    resp['inactivity_reasons'] = BulkCommonResources.populate_reasons(blocking_condition, reason_list)
                elif status_type == "bulk":
                    resp['imei'] = imei
                    resp['inactivity_reasons'] = BulkCommonResources.populate_reasons(blocking_condition, reason_list)
                    return resp
                return {"compliant": resp}
        except Exception as error:
            raise error

    @staticmethod
    def populate_reasons(blocking, reasons_list):
        """Return reasons for IMEI to be non compliant."""

        try:
            voilating_conditions = [key['condition_name'] for key in blocking if key['condition_met']]
            for condition in app.config['conditions']:
                if condition['name'] in voilating_conditions:
                    reasons_list.append(condition['reason'])
            return reasons_list
        except Exception as error:
            raise error

    # count IMEIs meeting no condition
    @staticmethod
    def no_condition_count(all_conditions):
        """Helper functions to generate summary, returns count of IMEI satisfying no conditions."""
        no_conditions = 0
        for key in all_conditions:
            if (~all_conditions[key]).all():
                no_conditions += 1
        return no_conditions
